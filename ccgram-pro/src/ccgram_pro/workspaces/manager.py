"""High-level workspace lifecycle — orchestrates clone/copy + install + sidecar bookkeeping.

This is the only module callers outside :mod:`ccgram_pro.workspaces`
should depend on; the strategy modules and install runner are
implementation details. Public surface:

- :func:`create_workspace` — provision a fresh workspace for a window.
- :func:`delete_workspace` — remove the workspace directory and clear the
  sidecar fields.
- :func:`touch_activity` — bump ``last_activity_at`` on the sidecar so
  the next idle GC sweep skips this window.

Sidecar mutation goes through :func:`ccgram_pro.state.transaction` so
concurrent callers serialize per-window.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from .. import state
from ..config import WorkspaceSettings, load_settings
from .copy_strategy import CopyError, copy_workspace
from .git_clone import GitCloneError, clone_workspace, is_git_repo
from .install import InstallResult, resolve_install_command, run_install
from .paths import workspace_for_window

logger = structlog.get_logger()


class WorkspaceCreationError(RuntimeError):
    """Raised when workspace provisioning fails before the sidecar is written."""


@dataclass(frozen=True)
class WorkspaceCreated:
    """Result returned to the caller of :func:`create_workspace`."""

    path: Path
    install: InstallResult | None


async def provision_workspace(
    source: Path,
    dest: Path,
    *,
    install_command: str | None = None,
    settings: WorkspaceSettings | None = None,
) -> WorkspaceCreated:
    """Clone/copy *source* into *dest* and run install — no sidecar write.

    Used by the new-session picker's "clone" strategy, which must provision
    the working directory *before* the tmux window (and therefore the
    window_id) exists. :func:`create_workspace` is the window-keyed wrapper
    that also records the sidecar.

    Failure modes mirror :func:`create_workspace`: a missing/wrong source or a
    clone/copy failure raises :class:`WorkspaceCreationError` (the partial
    directory is cleaned up by the strategy module); an install failure keeps
    the workspace and is surfaced via :attr:`WorkspaceCreated.install`.
    """
    if not source.is_dir():
        msg = f"Source project path does not exist or is not a directory: {source}"
        raise WorkspaceCreationError(msg)
    if dest.exists():
        msg = f"Workspace destination already exists: {dest}"
        raise WorkspaceCreationError(msg)

    ws_settings = settings or load_settings().workspaces
    strategy = ws_settings.strategy
    if strategy == "clone" and not is_git_repo(source):
        logger.debug(
            "Source %s is not a git repo; using copy strategy instead of clone",
            source,
        )
        strategy = "copy"

    try:
        if strategy == "clone":
            await clone_workspace(
                source,
                dest,
                transfer_uncommitted=ws_settings.transfer_uncommitted,
            )
        else:
            await copy_workspace(source, dest)
    except (GitCloneError, CopyError) as exc:
        raise WorkspaceCreationError(str(exc)) from exc

    install_result: InstallResult | None = None
    command = resolve_install_command(dest, configured=install_command)
    if command is not None:
        try:
            install_result = await run_install(
                dest,
                command,
                timeout_seconds=ws_settings.install_timeout_seconds,
            )
        except (ValueError, OSError) as exc:
            logger.warning("Install run failed to start for %s: %s", dest, exc)

    if install_result is not None and not install_result.succeeded:
        logger.warning(
            "Install for %s exited %d in %.1fs; see %s",
            dest,
            install_result.returncode,
            install_result.duration_seconds,
            install_result.log_path,
        )
    logger.info("Provisioned workspace at %s (strategy=%s)", dest, strategy)
    return WorkspaceCreated(path=dest, install=install_result)


async def create_workspace(
    window_id: str,
    source: Path,
    *,
    install_command: str | None = None,
    settings: WorkspaceSettings | None = None,
) -> WorkspaceCreated:
    """Provision a per-window workspace from *source*.

    Strategy is taken from *settings* (defaults loaded from
    ``settings.toml``) — ``"clone"`` for git sources, falling back to
    ``"copy"`` when the source isn't a git work-tree. On success, the
    sidecar for *window_id* is updated with ``workspace_path`` and
    ``last_activity_at`` inside a per-window transaction.

    Failure modes:

    - source missing / wrong shape → :class:`WorkspaceCreationError`.
    - clone / copy step fails → :class:`WorkspaceCreationError` (and the
      partial workspace is cleaned up by the strategy module).
    - install step fails → the workspace is kept; the failure is logged
      and surfaced via :attr:`WorkspaceCreated.install`. The caller
      decides what to do (Phase 1 surfaces it to Telegram).
    """
    workspace = workspace_for_window(window_id)
    if workspace.exists():
        msg = f"Workspace already exists for window {window_id}: {workspace}"
        raise WorkspaceCreationError(msg)

    created = await provision_workspace(
        source, workspace, install_command=install_command, settings=settings
    )

    now = time.time()
    async with state.transaction(window_id):
        sidecar = state.get_or_create(window_id)
        sidecar.workspace_path = str(workspace)
        sidecar.last_activity_at = now
        # Recording the resolved project path even when the picker hasn't
        # run yet keeps the sidecar self-describing for GC + doctor.
        if sidecar.project_path is None:
            sidecar.project_path = str(source)
        state.save(sidecar)

    logger.info("Provisioned workspace for window %s at %s", window_id, workspace)
    return WorkspaceCreated(path=workspace, install=created.install)


async def delete_workspace(window_id: str) -> bool:
    """Remove the workspace for *window_id* and clear the sidecar fields.

    Returns ``True`` when a workspace was actually removed, ``False`` when
    none existed. The directory removal is best-effort — read-only files
    inside a node_modules tree have been known to defeat ``rmtree``, so
    failures are logged and the function still clears the sidecar bookkeeping
    so the orphan is visible to the GC sweep.

    The path comes from the sidecar's recorded ``workspace_path`` — NOT
    ``workspace_for_window(window_id)`` — because the new-session picker's
    clone strategy provisions to a ``pending-<uuid>`` directory that does not
    follow the window-id naming. Falls back to the window-id path only when no
    sidecar path is recorded (legacy / pre-pick provisioning).
    """
    sidecar = state.load(window_id)
    recorded = (
        Path(sidecar.workspace_path)
        if sidecar is not None and sidecar.workspace_path
        else workspace_for_window(window_id)
    )
    removed = False
    if recorded.exists():
        try:
            shutil.rmtree(recorded)
            removed = True
        except OSError as exc:
            logger.warning("Failed to remove workspace %s: %s", recorded, exc)

    async with state.transaction(window_id):
        sidecar = state.load(window_id)
        if sidecar is not None and (
            sidecar.workspace_path is not None or sidecar.last_activity_at is not None
        ):
            sidecar.workspace_path = None
            sidecar.last_activity_at = None
            state.save(sidecar)

    if removed:
        logger.info("Removed workspace for window %s", window_id)
    return removed


async def touch_activity(window_id: str) -> None:
    """Refresh the sidecar's ``last_activity_at`` to now.

    Called whenever the layer registers user-visible activity for the
    window (Phase 1+ will hook this into message forwarding). Safe to
    call on a window with no workspace — it just records that the
    sidecar saw activity.
    """
    async with state.transaction(window_id):
        sidecar = state.load(window_id)
        if sidecar is None:
            return
        sidecar.last_activity_at = time.time()
        state.save(sidecar)

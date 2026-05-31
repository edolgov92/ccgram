"""Full session teardown when a Telegram topic is deleted.

Registered as a ``window``-scope cleanup on ccgram's ``TopicStateRegistry``,
which fires both when a topic is *deleted* (probe → kill window →
``clear_topic_state`` → ``clear_window``) and when an unbound window is
auto-killed past its TTL (``clear_window`` directly). Both kill the window
first, so destructive teardown is gated on the window being truly GONE — a mere
topic *close* (window kept alive for rebind) also fires ``clear_window`` and must
NOT reclaim anything.

Reclaims, by workspace strategy:

- ``worktree`` → ``git worktree remove --force`` against the source repo.
- ``clone`` / ``copy`` → ``rmtree`` the layer-owned workspace dir.
- ``current`` → nothing (it's the user's real source tree).

Plus the diff-snapshot dir + git refs, the layer sidecar, and the window's share
records. NEVER touches external/emdash windows or branches with unmerged work.
The idle/orphan GC remains the backstop for any missed hook.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import structlog

from . import state
from .config import load_settings

logger = structlog.get_logger()

_installed = False


def install_session_teardown(_application: Any) -> None:
    """Register the window-scope teardown cleanup on ccgram's registry."""
    global _installed
    if _installed:
        return
    # Lazy: ccgram registry is wired during bootstrap.
    from ccgram.topic_state_registry import topic_state

    topic_state.register("window")(_on_window_cleared)
    _installed = True
    logger.info("ccgram-pro session teardown installed (window-scope cleanup)")


def _on_window_cleared(window_id: str) -> None:
    """Sync registry callback — schedule async teardown on the running loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_maybe_teardown(window_id))


async def _maybe_teardown(window_id: str) -> None:
    """Tear down only when the window is truly gone and is ours (not external)."""
    # Lazy: ccgram internals — deferred to avoid a bootstrap import cycle.
    from ccgram.tmux_manager import tmux_manager

    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.window_resolver import is_foreign_window

    if is_foreign_window(window_id):
        return  # external/emdash — never our resource to reclaim
    with contextlib.suppress(Exception):
        if await tmux_manager.find_window_by_id(window_id) is not None:
            return  # window still alive → topic close/unbind, not a death
    try:
        await _teardown_session_resources(window_id)
    except Exception:  # noqa: BLE001 -- teardown must never crash the cleanup path
        logger.exception("session teardown failed for %s", window_id)


async def _teardown_session_resources(window_id: str) -> None:
    sidecar = state.load(window_id)
    if sidecar is None:
        return  # nothing the layer provisioned (or already cleaned)

    # Snapshots first — delete the git refs while the repo/worktree still exists.
    await _drop_snapshots(window_id)
    await _drop_workspace(window_id, sidecar)
    _drop_shares(window_id)
    if load_settings().defaults.delete_transcript_on_teardown:
        await _drop_transcript(window_id)
    state.delete(window_id)
    logger.info(
        "session teardown complete: window=%s strategy=%s",
        window_id,
        sidecar.workspace_strategy,
    )


async def _drop_snapshots(window_id: str) -> None:
    # Lazy: git_ops pulls subprocess; only needed on teardown.
    from .git_ops.snapshot import delete_window_snapshots

    with contextlib.suppress(Exception):
        await asyncio.to_thread(delete_window_snapshots, window_id)


async def _drop_workspace(window_id: str, sidecar: state.WindowSidecar) -> None:
    strategy = sidecar.workspace_strategy
    if strategy == "worktree":
        await _remove_worktree(sidecar)
    elif strategy in ("clone", "copy"):
        # Lazy: workspaces manager pulls git/rsync; only needed on teardown.
        from .workspaces.manager import delete_workspace

        with contextlib.suppress(Exception):
            await delete_workspace(window_id)
    # "current" → never delete the user's real repository.


async def _remove_worktree(sidecar: state.WindowSidecar) -> None:
    """``git worktree remove --force`` the worktree (never delete its branch)."""
    source = sidecar.source_repo_path
    worktree = sidecar.workspace_path
    if not source or not worktree:
        return
    # Lazy: git_ops pulls subprocess; only needed on teardown.
    from .git_ops._run import run_git

    def _remove() -> None:
        # ``--force`` removes even a dirty worktree (the session is over). We do
        # NOT delete the branch: it may carry unmerged commits — the user can
        # drop it deliberately. ``check=False`` so a missing worktree is a no-op.
        run_git(source, "worktree", "remove", "--force", worktree, check=False)
        run_git(source, "worktree", "prune", check=False)

    with contextlib.suppress(Exception):
        await asyncio.to_thread(_remove)


def _drop_shares(window_id: str) -> None:
    # Lazy: share store is layer-internal but heavy-ish; defer to teardown.
    from .share.store import delete_shares_for_window

    with contextlib.suppress(Exception):
        delete_shares_for_window(window_id)


async def _drop_transcript(window_id: str) -> None:
    """Opt-in: delete the current Claude transcript file for *window_id*.

    Resolves the path from ``session_map.json`` (the window is already gone, so
    the live window view is unavailable). Off by default — recovery normally
    wants the transcript kept.
    """
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.session_map import read_session_map_raw

    with contextlib.suppress(Exception):
        raw = await read_session_map_raw()
        if not raw:
            return
        transcript_path = _transcript_path_for(raw, window_id)
        if transcript_path:
            await asyncio.to_thread(_unlink, transcript_path)


def _transcript_path_for(raw: dict[str, Any], window_id: str) -> str | None:
    suffix = f":{window_id}"
    for key, info in raw.items():
        if key.endswith(suffix) and isinstance(info, dict):
            path = info.get("transcript_path")
            if isinstance(path, str) and path:
                return path
    return None


def _unlink(path: str) -> None:
    with contextlib.suppress(OSError):
        Path(path).unlink()


def _reset_for_testing() -> None:
    global _installed
    _installed = False

"""Idle-workspace garbage collection.

Workspaces are deleted on two conditions:

1. **Idle.** The owning sidecar's ``last_activity_at`` is older than
   ``WorkspaceSettings.idle_days``. The user explicitly designed this as
   the "session has ended" signal — wall-clock idle is the only reliable
   proxy when neither tmux nor Telegram emit a clear lifecycle event.
2. **Orphan.** A workspace directory exists on disk but no sidecar
   references it. This catches the case where a sidecar was deleted (GC,
   epoch fingerprint mismatch, manual ``rm``) while the workspace was
   left behind.

The sweep runs synchronously inside ``asyncio.to_thread`` from the
runtime job. Each removal is independent — one failure does not block
the rest of the sweep.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from .. import state
from ..config import WorkspaceSettings, load_settings, workspaces_dir
from .paths import workspace_for_window

logger = structlog.get_logger()


@dataclass(frozen=True)
class SweepResult:
    """Counts returned from a single GC pass — surfaced by doctor."""

    idle_removed: int
    orphans_removed: int

    @property
    def total(self) -> int:
        return self.idle_removed + self.orphans_removed


def _seconds(days: int) -> int:
    return days * 24 * 60 * 60


def _try_rmtree(path: Path) -> bool:
    try:
        shutil.rmtree(path)
    except OSError as exc:
        logger.warning("GC could not remove %s: %s", path, exc)
        return False
    return True


def _sweep_idle(now: float, idle_seconds: int) -> int:
    """Remove workspaces whose sidecar's last_activity_at is older than the threshold."""
    removed = 0
    for sidecar in state.all_sidecars():
        if sidecar.workspace_path is None:
            continue
        if sidecar.last_activity_at is None:
            # Sidecar references a workspace but never recorded activity —
            # treat as orphan-ish, but don't aggressively delete since the
            # workspace may have been created seconds ago. Skip; the next
            # touch_activity call will populate the timestamp.
            continue
        if now - sidecar.last_activity_at <= idle_seconds:
            continue
        workspace = Path(sidecar.workspace_path)
        if workspace.exists() and not _try_rmtree(workspace):
            continue
        # Clear sidecar fields whether or not the dir existed — the sidecar
        # bookkeeping is the source of truth for "we have a workspace".
        sidecar.workspace_path = None
        sidecar.last_activity_at = None
        state.save(sidecar)
        logger.info(
            "GC removed idle workspace %s (window=%s)", workspace, sidecar.window_id
        )
        removed += 1
    return removed


def _sweep_orphans() -> int:
    """Remove workspace directories that no sidecar references."""
    root = workspaces_dir()
    if not root.is_dir():
        return 0
    sidecar_paths = {
        sidecar.workspace_path
        for sidecar in state.all_sidecars()
        if sidecar.workspace_path is not None
    }
    removed = 0
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        # Skip staging dirs that strategy modules use during provisioning —
        # they have a ``.stage-`` suffix and a live writer.
        if ".stage-" in entry.name:
            continue
        # Also defend against the sanitize_dir_name round-trip: an entry
        # we don't recognise as a known window_id stays UNLESS no sidecar
        # claims its absolute path.
        if str(entry) in sidecar_paths:
            continue
        if _try_rmtree(entry):
            logger.info("GC removed orphan workspace %s", entry)
            removed += 1
    return removed


def sweep(
    *,
    now: float | None = None,
    settings: WorkspaceSettings | None = None,
) -> SweepResult:
    """Run one GC pass.

    The two phases are independent — orphan cleanup also runs even when
    no idle candidates exist, since the most common orphan cause is a
    sidecar deleted out from under a workspace.
    """
    effective_now = time.time() if now is None else now
    effective_settings = settings or load_settings().workspaces
    idle_seconds = _seconds(effective_settings.idle_days)
    idle_removed = _sweep_idle(effective_now, idle_seconds)
    orphans_removed = _sweep_orphans()
    return SweepResult(idle_removed=idle_removed, orphans_removed=orphans_removed)


def expected_workspace_for(window_id: str) -> Path:
    """Helper for diagnostics — the path :func:`workspace_for_window` would resolve."""
    return workspace_for_window(window_id)

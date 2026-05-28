"""Tests for ``ccgram_pro.workspaces.gc`` — idle + orphan sweep."""

from __future__ import annotations

import time
from pathlib import Path

from ccgram_pro import state
from ccgram_pro.config import (
    WorkspaceSettings,
    ensure_layer_dirs,
    workspaces_dir,
)
from ccgram_pro.workspaces import gc
from ccgram_pro.workspaces.paths import workspace_for_window


def _make_sidecar_with_workspace(window_id: str, *, age_days: int) -> Path:
    ensure_layer_dirs()
    ws = workspace_for_window(window_id)
    ws.mkdir(parents=True)
    (ws / "marker").write_text("x", encoding="utf-8")
    sidecar = state.WindowSidecar(
        window_id=window_id,
        window_creation_epoch=0.0,
        workspace_path=str(ws),
        last_activity_at=time.time() - age_days * 86400,
    )
    state.save(sidecar)
    return ws


def test_sweep_removes_idle_workspaces(tmp_path: Path) -> None:
    old = _make_sidecar_with_workspace("@old", age_days=10)
    young = _make_sidecar_with_workspace("@young", age_days=1)
    result = gc.sweep(settings=WorkspaceSettings(idle_days=5))
    assert result.idle_removed == 1
    assert not old.exists()
    assert young.exists()
    # Sidecar fields cleared on the idle one.
    cleared = state.load("@old")
    assert cleared is not None
    assert cleared.workspace_path is None
    assert cleared.last_activity_at is None
    # Untouched on the live one.
    survivor = state.load("@young")
    assert survivor is not None
    assert survivor.workspace_path is not None


def test_sweep_idle_skips_sidecar_with_no_workspace(tmp_path: Path) -> None:
    sidecar = state.WindowSidecar(window_id="@no-ws", window_creation_epoch=0.0)
    state.save(sidecar)
    result = gc.sweep(settings=WorkspaceSettings(idle_days=5))
    assert result.idle_removed == 0


def test_sweep_idle_skips_sidecar_with_no_activity_timestamp(tmp_path: Path) -> None:
    """A workspace without last_activity_at is treated as "just created", not orphan."""
    ensure_layer_dirs()
    ws = workspace_for_window("@just-made")
    ws.mkdir(parents=True)
    sidecar = state.WindowSidecar(
        window_id="@just-made",
        window_creation_epoch=0.0,
        workspace_path=str(ws),
        last_activity_at=None,
    )
    state.save(sidecar)
    result = gc.sweep(settings=WorkspaceSettings(idle_days=5))
    assert result.idle_removed == 0
    assert ws.exists()


def test_sweep_removes_orphan_workspace_directories(tmp_path: Path) -> None:
    """Workspaces on disk without an owning sidecar should be cleaned up."""
    ensure_layer_dirs()
    orphan = workspaces_dir() / "orphan-window"
    orphan.mkdir(parents=True)
    (orphan / "stuff").write_text("noise", encoding="utf-8")
    result = gc.sweep(settings=WorkspaceSettings())
    assert result.orphans_removed == 1
    assert not orphan.exists()


def test_sweep_does_not_remove_stage_dirs(tmp_path: Path) -> None:
    """Active staging dirs from a concurrent provisioning must survive sweep."""
    ensure_layer_dirs()
    stage = workspaces_dir() / "@x.stage-abc"
    stage.mkdir(parents=True)
    result = gc.sweep(settings=WorkspaceSettings())
    assert stage.exists()
    assert result.orphans_removed == 0


def test_sweep_total_combines_idle_and_orphans(tmp_path: Path) -> None:
    _make_sidecar_with_workspace("@old1", age_days=10)
    _make_sidecar_with_workspace("@old2", age_days=12)
    ensure_layer_dirs()
    (workspaces_dir() / "orphan").mkdir(parents=True)
    result = gc.sweep(settings=WorkspaceSettings(idle_days=5))
    assert result.idle_removed == 2
    assert result.orphans_removed == 1
    assert result.total == 3


def test_sweep_handles_missing_workspaces_dir(tmp_path: Path, monkeypatch) -> None:
    """A fresh layout with no workspaces_dir() yet must not crash the GC."""
    # Don't call ensure_layer_dirs — leave workspaces_dir absent.
    result = gc.sweep(settings=WorkspaceSettings())
    assert result.total == 0


def test_sweep_now_override_is_respected(tmp_path: Path) -> None:
    _make_sidecar_with_workspace("@frozen", age_days=0)
    # Force "now" to be 100 days in the future; sidecar becomes very stale.
    far_future = time.time() + 100 * 86400
    result = gc.sweep(now=far_future, settings=WorkspaceSettings(idle_days=5))
    assert result.idle_removed == 1

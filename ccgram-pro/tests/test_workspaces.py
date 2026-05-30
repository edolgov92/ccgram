"""Tests for ``ccgram_pro.workspaces`` — clone, copy, install detect, manager."""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest
from ccgram_pro import state
from ccgram_pro.config import (
    WorkspaceSettings,
    ensure_layer_dirs,
    workspaces_dir,
)
from ccgram_pro.workspaces import manager
from ccgram_pro.workspaces.copy_strategy import copy_workspace
from ccgram_pro.workspaces.git_clone import (
    clone_workspace,
    is_git_repo,
)
from ccgram_pro.workspaces.install import (
    detect_install_command,
    resolve_install_command,
    run_install,
)
from ccgram_pro.workspaces.paths import workspace_for_window


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _make_git_repo(root: Path, *, name: str = "src") -> Path:
    """Create a tiny git repo with one commit and an uncommitted edit + untracked file."""
    src = root / name
    src.mkdir(parents=True, exist_ok=True)
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "test@example.com")
    _git(src, "config", "user.name", "Test")
    (src / "README.md").write_text("# initial\n", encoding="utf-8")
    (src / "package.json").write_text('{"name":"t"}\n', encoding="utf-8")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "initial")
    # Uncommitted edit + untracked file the clone should optionally carry over.
    (src / "README.md").write_text("# initial\n\nuncommitted\n", encoding="utf-8")
    (src / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    return src


def test_is_git_repo_detects_repo(tmp_path: Path) -> None:
    src = _make_git_repo(tmp_path)
    assert is_git_repo(src) is True


def test_is_git_repo_false_for_plain_dir(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    assert is_git_repo(tmp_path / "plain") is False


def test_is_git_repo_false_for_missing_path(tmp_path: Path) -> None:
    assert is_git_repo(tmp_path / "absent") is False


async def test_clone_workspace_carries_head(tmp_path: Path) -> None:
    src = _make_git_repo(tmp_path)
    dest = workspaces_dir() / "@w1"
    ensure_layer_dirs()
    await clone_workspace(src, dest, transfer_uncommitted=False)
    assert (dest / "README.md").read_text() == "# initial\n"
    assert not (dest / "scratch.txt").exists()  # untracked, didn't transfer


async def test_clone_workspace_carries_uncommitted_and_untracked(
    tmp_path: Path,
) -> None:
    src = _make_git_repo(tmp_path)
    dest = workspaces_dir() / "@w2"
    ensure_layer_dirs()
    await clone_workspace(src, dest, transfer_uncommitted=True)
    assert "uncommitted" in (dest / "README.md").read_text()
    assert (dest / "scratch.txt").read_text() == "untracked\n"


async def test_clone_workspace_refuses_existing_destination(tmp_path: Path) -> None:
    src = _make_git_repo(tmp_path)
    dest = workspaces_dir() / "@w3"
    ensure_layer_dirs()
    dest.mkdir(parents=True)
    from ccgram_pro.workspaces.git_clone import GitCloneError

    with pytest.raises(GitCloneError):
        await clone_workspace(src, dest, transfer_uncommitted=False)


async def test_copy_workspace_uses_excludes(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "keep.txt").write_text("k\n", encoding="utf-8")
    (src / "node_modules").mkdir()
    (src / "node_modules" / "x").write_text("noise\n", encoding="utf-8")
    dest = workspaces_dir() / "@w4"
    ensure_layer_dirs()
    await copy_workspace(src, dest)
    assert (dest / "keep.txt").exists()
    assert not (dest / "node_modules").exists()


async def test_copy_workspace_refuses_missing_source(tmp_path: Path) -> None:
    dest = workspaces_dir() / "@w5"
    ensure_layer_dirs()
    from ccgram_pro.workspaces.copy_strategy import CopyError

    with pytest.raises(CopyError):
        await copy_workspace(tmp_path / "nope", dest)


def test_detect_install_command_pnpm(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name":"t"}', encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    assert detect_install_command(tmp_path) == "pnpm install --frozen-lockfile"


def test_detect_install_command_uv(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    assert detect_install_command(tmp_path) == "uv sync"


def test_detect_install_command_npm_no_lock(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name":"t"}', encoding="utf-8")
    assert detect_install_command(tmp_path) == "npm install"


def test_detect_install_command_none_when_unknown(tmp_path: Path) -> None:
    (tmp_path / "main.rs").write_text("fn main() {}", encoding="utf-8")
    assert detect_install_command(tmp_path) is None


def test_resolve_install_command_skip_on_empty_string(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name":"t"}', encoding="utf-8")
    assert resolve_install_command(tmp_path, configured="") is None


def test_resolve_install_command_user_override(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name":"t"}', encoding="utf-8")
    assert resolve_install_command(tmp_path, configured="echo custom") == "echo custom"


def test_resolve_install_command_autodetect_when_not_configured(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    assert resolve_install_command(tmp_path, configured=None) == "uv sync"


async def test_run_install_captures_output_and_succeeds(tmp_path: Path) -> None:
    ensure_layer_dirs()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = await run_install(workspace, "echo hi", timeout_seconds=10)
    assert result.succeeded
    log = (workspace / ".ccgram-install.log").read_text()
    assert "hi" in log
    assert "$ echo hi" in log


async def test_run_install_records_nonzero_exit(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = await run_install(workspace, "sh -c 'exit 5'", timeout_seconds=10)
    assert result.returncode == 5
    assert not result.succeeded


async def test_run_install_command_not_found(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = await run_install(
        workspace, "definitely-not-a-real-cmd-xyz", timeout_seconds=10
    )
    assert result.returncode == 127
    log = (workspace / ".ccgram-install.log").read_text()
    assert "command not found" in log


async def test_run_install_times_out(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = await run_install(workspace, "sleep 10", timeout_seconds=1)
    assert result.returncode == 124
    log = (workspace / ".ccgram-install.log").read_text()
    assert "killed after" in log


async def test_create_workspace_clones_and_writes_sidecar(tmp_path: Path) -> None:
    src = _make_git_repo(tmp_path)
    created = await manager.create_workspace(
        "@m1",
        src,
        install_command="",  # skip install
        settings=WorkspaceSettings(strategy="clone", transfer_uncommitted=True),
    )
    assert created.path == workspace_for_window("@m1")
    assert created.path.exists()
    assert (created.path / "scratch.txt").read_text() == "untracked\n"
    sidecar = state.load("@m1")
    assert sidecar is not None
    assert sidecar.workspace_path == str(created.path)
    assert sidecar.last_activity_at is not None
    assert sidecar.project_path == str(src)


async def test_create_workspace_falls_back_to_copy_for_non_git(tmp_path: Path) -> None:
    src = tmp_path / "plain"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")
    created = await manager.create_workspace(
        "@m2",
        src,
        install_command="",
        settings=WorkspaceSettings(strategy="clone"),  # auto-downgrades
    )
    assert (created.path / "a.txt").read_text() == "hi"


async def test_create_workspace_refuses_missing_source(tmp_path: Path) -> None:
    with pytest.raises(manager.WorkspaceCreationError):
        await manager.create_workspace("@m3", tmp_path / "absent")


async def test_create_workspace_refuses_existing_workspace(tmp_path: Path) -> None:
    src = _make_git_repo(tmp_path)
    workspace = workspace_for_window("@m4")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    workspace.mkdir()
    with pytest.raises(manager.WorkspaceCreationError):
        await manager.create_workspace("@m4", src, install_command="")


async def test_delete_workspace_removes_dir_and_clears_sidecar(tmp_path: Path) -> None:
    src = _make_git_repo(tmp_path)
    await manager.create_workspace(
        "@m5", src, install_command="", settings=WorkspaceSettings()
    )
    sidecar_before = state.load("@m5")
    assert sidecar_before is not None
    assert sidecar_before.workspace_path is not None
    removed = await manager.delete_workspace("@m5")
    assert removed is True
    assert not workspace_for_window("@m5").exists()
    sidecar_after = state.load("@m5")
    assert sidecar_after is not None
    assert sidecar_after.workspace_path is None
    assert sidecar_after.last_activity_at is None


async def test_delete_workspace_returns_false_when_missing(tmp_path: Path) -> None:
    assert await manager.delete_workspace("@never-existed") is False


async def test_delete_workspace_removes_clone_path_not_window_path(
    tmp_path: Path,
) -> None:
    """The new-session clone strategy stores a pending-<uuid> path that does
    NOT match workspace_for_window — delete_workspace must honour the stored
    sidecar path, not the window-id-derived one."""
    from ccgram_pro.config import workspaces_dir

    clone = workspaces_dir() / "pending-deadbeef"
    clone.mkdir(parents=True)
    (clone / "f").write_text("x", encoding="utf-8")
    state.save(
        state.WindowSidecar(
            window_id="@clone1",
            window_creation_epoch=0.0,
            workspace_path=str(clone),
            last_activity_at=0.0,
        )
    )
    # The window-id-derived path is a different (non-existent) location.
    assert not workspace_for_window("@clone1").exists()
    removed = await manager.delete_workspace("@clone1")
    assert removed is True
    assert not clone.exists()
    sidecar = state.load("@clone1")
    assert sidecar is not None
    assert sidecar.workspace_path is None


async def test_touch_activity_updates_sidecar(tmp_path: Path) -> None:
    state.save(state.WindowSidecar(window_id="@touch", window_creation_epoch=0.0))
    before = time.time()
    await asyncio.sleep(0.01)
    await manager.touch_activity("@touch")
    sidecar = state.load("@touch")
    assert sidecar is not None
    assert sidecar.last_activity_at is not None
    assert sidecar.last_activity_at >= before


async def test_touch_activity_no_op_when_sidecar_missing() -> None:
    # Should NOT raise.
    await manager.touch_activity("@no-sidecar")

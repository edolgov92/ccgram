from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from ccgram_pro import session_teardown, state
from ccgram_pro.git_ops import capture_snapshot, load_index
from ccgram_pro.share.store import (
    ShareNotFound,
    delete_shares_for_window,
    load_share,
    save_share,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@e.com")
    _git(path, "config", "user.name", "T")
    (path / "README.md").write_text("hello\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


async def test_teardown_current_strategy_keeps_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "proj")
    capture_snapshot(window_id="@cur", project_root=repo)
    share_id = save_share(kind="plan", title="p", body_markdown="x", window_id="@cur")
    sidecar = state.WindowSidecar(window_id="@cur", window_creation_epoch=0.0)
    sidecar.workspace_strategy = "current"
    sidecar.project_path = str(repo)
    state.save(sidecar)

    await session_teardown._teardown_session_resources("@cur")

    assert repo.exists() and (repo / "README.md").exists()  # repo untouched
    assert load_index("@cur") is None  # snapshots gone
    assert state.load("@cur") is None  # sidecar gone
    with pytest.raises(ShareNotFound):
        load_share(share_id)


async def test_teardown_clone_rmtrees_workspace(tmp_path: Path) -> None:
    clone = _init_repo(tmp_path / "clone")
    capture_snapshot(window_id="@cl", project_root=clone)
    sidecar = state.WindowSidecar(window_id="@cl", window_creation_epoch=0.0)
    sidecar.workspace_strategy = "clone"
    sidecar.workspace_path = str(clone)
    state.save(sidecar)

    await session_teardown._teardown_session_resources("@cl")

    assert not clone.exists()  # layer-owned clone removed
    assert state.load("@cl") is None


async def test_teardown_worktree_removes_worktree(tmp_path: Path) -> None:
    source = _init_repo(tmp_path / "src")
    worktree = tmp_path / "wt"
    _git(source, "worktree", "add", "-q", "-b", "feature", str(worktree))
    capture_snapshot(window_id="@wt", project_root=worktree)
    sidecar = state.WindowSidecar(window_id="@wt", window_creation_epoch=0.0)
    sidecar.workspace_strategy = "worktree"
    sidecar.source_repo_path = str(source)
    sidecar.workspace_path = str(worktree)
    state.save(sidecar)

    await session_teardown._teardown_session_resources("@wt")

    assert not worktree.exists()  # worktree dir removed
    listed = subprocess.run(
        ["git", "-C", str(source), "worktree", "list"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "wt" not in listed  # no stale worktree registration
    assert source.exists() and (source / "README.md").exists()  # source intact


async def test_teardown_missing_sidecar_noop() -> None:
    await session_teardown._teardown_session_resources("@nope")  # must not raise


async def test_gate_skips_when_window_live(monkeypatch) -> None:
    import ccgram.tmux_manager as tm
    import ccgram.window_resolver as wr

    called: list = []

    async def spy(window_id):
        called.append(window_id)

    monkeypatch.setattr(session_teardown, "_teardown_session_resources", spy)
    monkeypatch.setattr(wr, "is_foreign_window", lambda wid: False)

    async def _find(window_id):
        return SimpleNamespace(window_id=window_id)  # window still alive

    monkeypatch.setattr(tm, "tmux_manager", SimpleNamespace(find_window_by_id=_find))
    await session_teardown._maybe_teardown("@1")
    assert called == []  # never tore down a live window


async def test_gate_skips_external_window(monkeypatch) -> None:
    import ccgram.window_resolver as wr

    called: list = []

    async def spy(window_id):
        called.append(window_id)

    monkeypatch.setattr(session_teardown, "_teardown_session_resources", spy)
    monkeypatch.setattr(wr, "is_foreign_window", lambda wid: True)
    await session_teardown._maybe_teardown("emdash-x:@0")
    assert called == []


async def test_gate_tears_down_dead_window(monkeypatch) -> None:
    import ccgram.tmux_manager as tm
    import ccgram.window_resolver as wr

    called: list = []

    async def spy(window_id):
        called.append(window_id)

    monkeypatch.setattr(session_teardown, "_teardown_session_resources", spy)
    monkeypatch.setattr(wr, "is_foreign_window", lambda wid: False)

    async def _find(window_id):
        return None  # window gone

    monkeypatch.setattr(tm, "tmux_manager", SimpleNamespace(find_window_by_id=_find))
    await session_teardown._maybe_teardown("@1")
    assert called == ["@1"]


def test_delete_shares_for_window_only_matching() -> None:
    keep = save_share(kind="plan", title="k", body_markdown="x", window_id="@other")
    save_share(kind="plan", title="d", body_markdown="x", window_id="@target")
    removed = delete_shares_for_window("@target")
    assert removed == 1
    assert load_share(keep).window_id == "@other"  # unrelated share kept

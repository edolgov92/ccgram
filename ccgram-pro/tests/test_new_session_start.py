from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from ccgram_pro import new_session
from ccgram_pro import new_session_store as store
from ccgram_pro import state


@pytest.fixture(autouse=True)
def _reset():
    new_session._reset_for_testing()
    yield
    new_session._reset_for_testing()


@pytest.fixture
def projects_toml(tmp_path):
    from ccgram_pro.config import layer_dir

    layer_dir().mkdir(parents=True, exist_ok=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    (layer_dir() / "projects.toml").write_text(
        f'[[project]]\npath = "{proj}"\nlabel = "Proj"\n'
    )
    return proj


class _Query:
    def __init__(self) -> None:
        self.message = SimpleNamespace(
            message_id=1, chat=SimpleNamespace(id=10), message_thread_id=2
        )
        self.answers: list[Any] = []
        self.edits: list[str] = []
        self.deleted = False

    async def answer(self, *a: Any, **k: Any) -> None:
        self.answers.append((a, k))

    async def edit_message_text(self, *a: Any, **k: Any) -> None:
        self.edits.append(k.get("text", a[0] if a else ""))

    async def delete_message(self, *a: Any, **k: Any) -> None:
        self.deleted = True


def _ctx() -> Any:
    return SimpleNamespace(user_data={})


def _update() -> Any:
    return SimpleNamespace(effective_user=SimpleNamespace(id=7))


def _capture_cwb(monkeypatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def stub(
        query, user_id, selected_path, provider_name, approval_mode, context
    ):  # noqa: ARG001
        captured["cwd"] = selected_path
        captured["provider"] = provider_name
        captured["approval_mode"] = approval_mode
        captured["override_model"] = new_session._override_model
        captured["override_plan"] = new_session._override_plan

    import ccgram.handlers.topics.directory_callbacks as dc

    monkeypatch.setattr(dc, "_create_window_and_bind", stub)
    return captured


def _bind_wid(monkeypatch, wid: str | None) -> None:
    import ccgram.thread_router as tr

    monkeypatch.setattr(tr.thread_router, "get_window_for_thread", lambda uid, tid: wid)


async def test_current_repo_strategy(projects_toml, monkeypatch) -> None:
    captured = _capture_cwb(monkeypatch)
    _bind_wid(monkeypatch, "@7")
    s = store.create(10, 2, 7, "hello", default_mode="coding")
    s.workspace_strategy = "current"
    await new_session._handle_start(_Query(), _update(), _ctx(), s)
    assert captured["cwd"] == str(projects_toml)
    sidecar = state.load("@7")
    assert sidecar is not None
    assert sidecar.workspace_strategy == "current"
    assert sidecar.project_path == str(projects_toml)
    # store cleared on success
    assert store.get(10, 2) is None


async def test_plan_mode_sets_flags_and_normal_approval(
    projects_toml, monkeypatch
) -> None:
    captured = _capture_cwb(monkeypatch)
    _bind_wid(monkeypatch, "@7")
    s = store.create(10, 2, 7, "hello", default_mode="plan")
    await new_session._handle_start(_Query(), _update(), _ctx(), s)
    assert captured["approval_mode"] == "normal"
    assert captured["override_plan"] is True
    sidecar = state.load("@7")
    assert sidecar is not None
    assert sidecar.mode == "plan"
    assert sidecar.plan_mode == "entered"


async def test_clone_strategy(projects_toml, monkeypatch) -> None:
    captured = _capture_cwb(monkeypatch)
    _bind_wid(monkeypatch, "@7")

    async def stub_provision(source, dest, **kw):  # noqa: ARG001
        return SimpleNamespace(path=dest, install=None)

    import ccgram_pro.workspaces.manager as mgr

    monkeypatch.setattr(mgr, "provision_workspace", stub_provision)

    s = store.create(10, 2, 7, "hello", default_mode="coding")
    s.workspace_strategy = "clone"
    await new_session._handle_start(_Query(), _update(), _ctx(), s)
    sidecar = state.load("@7")
    assert sidecar is not None
    assert sidecar.workspace_strategy == "clone"
    assert sidecar.workspace_path == captured["cwd"]
    assert sidecar.last_activity_at is not None


async def test_clone_failure_keeps_picker(projects_toml, monkeypatch) -> None:
    _capture_cwb(monkeypatch)

    async def boom(source, dest, **kw):  # noqa: ARG001
        from ccgram_pro.workspaces.manager import WorkspaceCreationError

        raise WorkspaceCreationError("clone failed")

    import ccgram_pro.workspaces.manager as mgr

    monkeypatch.setattr(mgr, "provision_workspace", boom)
    s = store.create(10, 2, 7, "hello", default_mode="coding")
    s.workspace_strategy = "clone"
    q = _Query()
    await new_session._handle_start(q, _update(), _ctx(), s)
    # picker kept, in_progress cleared so user can retry
    assert store.get(10, 2) is s
    assert s.in_progress is False
    assert any("clone failed" in e for e in q.edits)


async def test_project_removed_clears_store(projects_toml, monkeypatch) -> None:
    s = store.create(10, 2, 7, "hello", default_mode="coding")
    s.project_idx = 99  # out of range
    q = _Query()
    await new_session._handle_start(q, _update(), _ctx(), s)
    assert store.get(10, 2) is None


async def test_window_id_resolved_from_binding(projects_toml, monkeypatch) -> None:
    _capture_cwb(monkeypatch)
    _bind_wid(monkeypatch, "@thread-bound")
    s = store.create(10, 2, 7, "hello", default_mode="coding")
    await new_session._handle_start(_Query(), _update(), _ctx(), s)
    # sidecar written for the binding-resolved id, NOT a guessed last window
    assert state.load("@thread-bound") is not None


async def test_start_deletes_picker_message_on_success(
    projects_toml, monkeypatch
) -> None:
    _capture_cwb(monkeypatch)
    _bind_wid(monkeypatch, "@7")
    s = store.create(10, 2, 7, "hello", default_mode="coding")
    q = _Query()
    await new_session._handle_start(q, _update(), _ctx(), s)
    assert q.deleted is True


async def test_start_keeps_card_on_bind_failure(projects_toml, monkeypatch) -> None:
    _capture_cwb(monkeypatch)
    _bind_wid(monkeypatch, None)
    s = store.create(10, 2, 7, "hello", default_mode="coding")
    q = _Query()
    await new_session._handle_start(q, _update(), _ctx(), s)
    assert q.deleted is False
    assert store.get(10, 2) is None


async def test_worktree_persists_path_and_source_repo(
    projects_toml, monkeypatch
) -> None:
    captured = _capture_cwb(monkeypatch)
    _bind_wid(monkeypatch, "@7")

    async def stub_provision_worktree(session, project, repo):  # noqa: ARG001
        from pathlib import Path

        return Path(str(projects_toml) + ".worktrees/feature")

    monkeypatch.setattr(new_session, "_provision_cwd", stub_provision_worktree)
    s = store.create(10, 2, 7, "hello", default_mode="coding")
    s.workspace_strategy = "worktree"
    await new_session._handle_start(_Query(), _update(), _ctx(), s)
    sidecar = state.load("@7")
    assert sidecar is not None
    assert sidecar.workspace_strategy == "worktree"
    assert sidecar.workspace_path == captured["cwd"]
    assert sidecar.source_repo_path == str(projects_toml)
    assert sidecar.last_activity_at is None

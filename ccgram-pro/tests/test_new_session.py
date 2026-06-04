from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from ccgram_pro import new_session
from ccgram_pro import new_session_store as store


@pytest.fixture(autouse=True)
def _reset():
    new_session._reset_for_testing()
    yield
    new_session._reset_for_testing()


@pytest.fixture
def projects_toml(tmp_path, monkeypatch):
    from ccgram_pro.config import layer_dir

    layer_dir().mkdir(parents=True, exist_ok=True)
    (layer_dir() / "projects.toml").write_text(
        '[[project]]\npath = "/tmp/a"\nlabel = "Project A"\n'
        '[[project]]\npath = "/tmp/b"\nlabel = "Project B"\n'
    )


def _session(**overrides):
    s = store.create(1, 2, 3, "hello", default_mode="coding")
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def test_apply_overrides_rewrites_existing_flags() -> None:
    cmd = "claude --model claude-opus-4-8 --effort xhigh --append-system-prompt 'x'"
    out = new_session._apply_overrides(cmd, "claude-opus-4-8[1m]", "max")
    assert "--model 'claude-opus-4-8[1m]'" in out
    assert "--effort max" in out
    assert "--effort xhigh" not in out
    assert "--append-system-prompt 'x'" in out


def test_apply_overrides_appends_when_absent() -> None:
    out = new_session._apply_overrides("claude", "claude-opus-4-8", "high")
    assert "--model claude-opus-4-8" in out
    assert "--effort high" in out


def test_apply_overrides_quotes_bracket_model() -> None:
    out = new_session._apply_overrides("claude --model x", "claude-opus-4-8[1m]", "low")
    assert "'claude-opus-4-8[1m]'" in out


def test_apply_overrides_adds_permission_mode_plan() -> None:
    out = new_session._apply_overrides("claude", "claude-opus-4-8", "high", plan=True)
    assert "--permission-mode plan" in out


def test_apply_overrides_no_plan_flag_when_coding() -> None:
    out = new_session._apply_overrides("claude", "claude-opus-4-8", "high", plan=False)
    assert "--permission-mode" not in out


def test_apply_overrides_rewrites_existing_permission_mode() -> None:
    out = new_session._apply_overrides(
        "claude --permission-mode acceptEdits", "claude-opus-4-8", "high", plan=True
    )
    assert "--permission-mode plan" in out
    assert "acceptEdits" not in out


def test_apply_overrides_appends_system_prompt_shlex_safe() -> None:
    out = new_session._apply_overrides(
        "claude", "claude-opus-4-8", "high", append_system_prompt="hello world\nline2"
    )
    import shlex

    tokens = shlex.split(out)
    assert "--append-system-prompt" in tokens
    assert "hello world\nline2" in tokens


def test_apply_overrides_does_not_duplicate_existing_prompt() -> None:
    cmd = "claude --append-system-prompt MARKER"
    out = new_session._apply_overrides(
        cmd, "claude-opus-4-8", "high", append_system_prompt="MARKER"
    )
    assert out.count("MARKER") == 1


def test_model_table_maps_keys() -> None:
    assert new_session._MODEL_STR["opus48"] == "claude-opus-4-8"
    assert new_session._MODEL_STR["opus48-1m"] == "claude-opus-4-8[1m]"


def test_build_keyboard_marks_selection(projects_toml) -> None:
    s = _session(project_idx=1, model_key="opus48-1m", effort_key="max", mode="plan")
    kb = new_session._build_keyboard(s)
    flat = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("🟢" in t and "Project B" in t for t in flat)
    assert any("📁" in t and "Project A" in t for t in flat)
    assert any(t.startswith("● ") and "1M" in t for t in flat)
    assert any(t == "● Max" for t in flat)
    assert any(t == "● Plan" for t in flat)
    assert any("Start" in t for t in flat)
    assert any(t == "Cancel" for t in flat)


def test_build_keyboard_callback_data(projects_toml) -> None:
    s = _session()
    kb = new_session._build_keyboard(s)
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "ccgrampro:new:project:0" in datas
    assert "ccgrampro:new:model:opus48-1m" in datas
    assert "ccgrampro:new:effort:max" in datas
    assert "ccgrampro:new:mode:plan" in datas
    assert "ccgrampro:new:ws:clone" in datas
    assert "ccgrampro:new:basemode:default" in datas
    assert "ccgrampro:new:basemode:current" in datas
    assert "ccgrampro:new:basemode:custom" in datas
    assert "ccgrampro:new:start" in datas
    assert "ccgrampro:new:cancel" in datas


def test_build_keyboard_hides_git_rows_for_non_git(projects_toml) -> None:
    s = _session(project_is_git=False)
    kb = new_session._build_keyboard(s)
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "ccgrampro:new:ws:worktree" not in datas
    assert "ccgrampro:new:basemode:default" not in datas
    assert "ccgrampro:new:ws:clone" in datas
    assert "ccgrampro:new:ws:current" in datas


def test_base_keyboard_uses_indices(projects_toml) -> None:
    s = _session(viewing_base=True, branch_choices=["main", "dev", "feat/x"])
    kb = new_session._build_keyboard(s)
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "ccgrampro:new:base:cur" in datas
    assert "ccgrampro:new:base:0" in datas
    assert "ccgrampro:new:base:2" in datas
    assert "ccgrampro:new:baseback" in datas


def test_render_text_shows_selection(projects_toml) -> None:
    s = _session(model_key="opus48-1m", effort_key="high", mode="plan")
    text = new_session._render_text(s)
    assert "Project A" in text
    assert "Opus 4.8 · 1M" in text
    assert "High" in text
    assert "Plan" in text


def _install_wrapped_unbound(projects_toml):
    """Install new_session and return (wrapped_unbound, restore-callable)."""
    import ccgram.handlers.text.text_handler as th
    import ccgram.providers as providers_mod

    orig_unbound = th._handle_unbound_topic
    orig_resolve = providers_mod.resolve_launch_command
    new_session.install_new_session(SimpleNamespace(add_handler=lambda *a, **k: None))
    wrapped = th._handle_unbound_topic

    def restore() -> None:
        th._handle_unbound_topic = orig_unbound
        providers_mod.resolve_launch_command = orig_resolve

    return wrapped, restore


def _stub_router(monkeypatch, window_id: str | None) -> None:
    """Replace the thread_router proxy (unwired in isolated tests) with a stub."""
    import ccgram.thread_router as tr

    monkeypatch.setattr(
        tr,
        "thread_router",
        SimpleNamespace(get_window_for_thread=lambda u, t: window_id),
    )


async def test_wrapped_unbound_skips_picker_when_topic_already_bound(
    projects_toml, monkeypatch
) -> None:
    """Regression: a bound topic must NOT re-show the picker / spawn a new window."""
    _stub_router(monkeypatch, "@9")
    wrapped, restore = _install_wrapped_unbound(projects_toml)
    try:
        msg = SimpleNamespace(chat=SimpleNamespace(id=1))
        handled = await wrapped(7, 100, "hello again", {}, msg)
        assert handled is False  # not handled here → text orchestrator forwards
        assert store.get(1, 100) is None  # no picker session created
    finally:
        restore()


async def test_wrapped_unbound_shows_picker_when_unbound(
    projects_toml, monkeypatch
) -> None:
    _stub_router(monkeypatch, None)
    calls: list[Any] = []

    async def stub_show(**kw: Any) -> bool:
        calls.append(kw)
        return True

    monkeypatch.setattr(new_session, "show_picker", stub_show)
    wrapped, restore = _install_wrapped_unbound(projects_toml)
    try:
        msg = SimpleNamespace(chat=SimpleNamespace(id=1))
        handled = await wrapped(7, 100, "hello", {}, msg)
        assert handled is True
        assert calls and calls[0]["thread_id"] == 100
    finally:
        restore()


# ── new-session modal rework: 2-col repos, base-mode row, branch status ──────


def test_projects_laid_out_two_per_row(projects_toml) -> None:
    s = _session()
    kb = new_session._build_keyboard(s)
    first_row = kb.inline_keyboard[0]
    assert len(first_row) == 2
    assert all(b.callback_data.startswith("ccgrampro:new:project:") for b in first_row)


def test_base_mode_row_locks_default_when_dirty() -> None:
    s = _session(project_is_git=True, default_branch_name="develop", is_dirty=True)
    row = new_session._base_mode_row(s)
    assert any("🔒" in b.text for b in row)
    cbs = [b.callback_data for b in row]
    assert "ccgrampro:new:basemode:default" in cbs
    assert "ccgrampro:new:basemode:current" in cbs
    assert "ccgrampro:new:basemode:custom" in cbs


def test_base_mode_row_default_selectable_when_clean() -> None:
    s = _session(
        project_is_git=True,
        default_branch_name="develop",
        is_dirty=False,
        has_unpushed=False,
        base_mode="default",
    )
    default_btn = new_session._base_mode_row(s)[0]
    assert "🔒" not in default_btn.text
    assert "develop" in default_btn.text
    assert default_btn.text.startswith("● ")


def test_render_text_shows_dirty_unpushed_branch_status() -> None:
    s = _session(
        project_is_git=True,
        current_branch_name="feature/x",
        is_dirty=True,
        has_unpushed=True,
        default_branch_name="develop",
    )
    text = new_session._render_text(s)
    assert "feature/x" in text
    assert "uncommitted" in text
    assert "unpushed" in text


def test_render_text_clean_branch_default_base() -> None:
    s = _session(
        project_is_git=True,
        current_branch_name="main",
        is_dirty=False,
        has_unpushed=False,
        default_branch_name="main",
        base_mode="default",
    )
    text = new_session._render_text(s)
    assert "clean" in text
    assert "switch + pull" in text


def test_effective_base_branch() -> None:
    assert (
        new_session._effective_base_branch(
            _session(base_mode="default", default_branch_name="develop")
        )
        == "develop"
    )
    assert new_session._effective_base_branch(_session(base_mode="current")) is None
    assert (
        new_session._effective_base_branch(
            _session(base_mode="custom", base_branch="feat/x")
        )
        == "feat/x"
    )


async def test_resolve_project_git_promotes_to_default_when_clean(
    projects_toml, monkeypatch
) -> None:
    monkeypatch.setattr(
        new_session,
        "_probe_git",
        lambda path: {
            "is_git": True,
            "current": "feature",
            "default": "develop",
            "dirty": False,
            "unpushed": False,
        },
    )
    s = _session()
    await new_session._resolve_project_git(s)
    assert s.base_mode == "default"
    assert s.default_branch_name == "develop"


async def test_resolve_project_git_stays_current_when_dirty(
    projects_toml, monkeypatch
) -> None:
    monkeypatch.setattr(
        new_session,
        "_probe_git",
        lambda path: {
            "is_git": True,
            "current": "feature",
            "default": "develop",
            "dirty": True,
            "unpushed": False,
        },
    )
    s = _session()
    await new_session._resolve_project_git(s)
    assert s.base_mode == "current"


class _BaseQuery:
    def __init__(self) -> None:
        self.answers: list[Any] = []
        self.edits: list[str] = []

    async def answer(self, text: str = "", **kw: Any) -> None:
        self.answers.append((text, kw))

    async def edit_message_text(self, *, text: str, **kw: Any) -> None:
        self.edits.append(text)


async def test_select_base_mode_default_blocked_when_dirty() -> None:
    s = _session(project_is_git=True, default_branch_name="develop", is_dirty=True)
    q = _BaseQuery()
    await new_session._select_base_mode(q, s, "default")
    assert s.base_mode != "default"  # rejected
    assert q.answers and q.answers[-1][1].get("show_alert") is True


async def test_select_base_mode_current_sets_mode() -> None:
    s = _session(
        project_is_git=True, default_branch_name="develop", base_mode="default"
    )
    q = _BaseQuery()
    await new_session._select_base_mode(q, s, "current")
    assert s.base_mode == "current"
    assert s.base_branch is None

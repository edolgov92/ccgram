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
    assert "ccgrampro:new:baseopen" in datas
    assert "ccgrampro:new:start" in datas
    assert "ccgrampro:new:cancel" in datas


def test_build_keyboard_hides_git_rows_for_non_git(projects_toml) -> None:
    s = _session(project_is_git=False)
    kb = new_session._build_keyboard(s)
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "ccgrampro:new:ws:worktree" not in datas
    assert "ccgrampro:new:baseopen" not in datas
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

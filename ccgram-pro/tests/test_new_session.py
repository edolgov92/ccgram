"""Tests for the one-step new-session picker (project + model + reasoning)."""

from __future__ import annotations

import pytest
from ccgram_pro import new_session


@pytest.fixture(autouse=True)
def _reset():
    new_session._reset_for_testing()
    yield
    new_session._reset_for_testing()


@pytest.fixture
def projects_toml(tmp_path, monkeypatch):
    """Two predefined projects under the isolated CCGRAM_DIR."""
    from ccgram_pro.config import layer_dir

    layer_dir().mkdir(parents=True, exist_ok=True)
    (layer_dir() / "projects.toml").write_text(
        '[[project]]\npath = "/tmp/a"\nlabel = "Project A"\n'
        '[[project]]\npath = "/tmp/b"\nlabel = "Project B"\n'
    )


def test_default_selection() -> None:
    sel = new_session._default_selection()
    assert sel == {"project": 0, "model": "opus48", "effort": "xhigh"}


def test_apply_overrides_rewrites_existing_flags() -> None:
    cmd = "claude --model claude-opus-4-8 --effort xhigh --append-system-prompt 'x'"
    out = new_session._apply_overrides(cmd, "claude-opus-4-8[1m]", "max")
    assert "--model 'claude-opus-4-8[1m]'" in out
    assert "--effort max" in out
    assert "--effort xhigh" not in out
    assert "--model claude-opus-4-8 " not in out
    # System prompt preserved.
    assert "--append-system-prompt 'x'" in out


def test_apply_overrides_appends_when_absent() -> None:
    out = new_session._apply_overrides("claude", "claude-opus-4-8", "high")
    assert "--model claude-opus-4-8" in out
    assert "--effort high" in out


def test_apply_overrides_quotes_bracket_model() -> None:
    out = new_session._apply_overrides("claude --model x", "claude-opus-4-8[1m]", "low")
    # Bracket model must be shell-quoted so [1m] isn't glob-expanded.
    assert "'claude-opus-4-8[1m]'" in out


def test_model_table_maps_keys() -> None:
    assert new_session._MODEL_STR["opus48"] == "claude-opus-4-8"
    assert new_session._MODEL_STR["opus48-1m"] == "claude-opus-4-8[1m]"


def test_build_keyboard_marks_selection(projects_toml) -> None:
    sel = {"project": 1, "model": "opus48-1m", "effort": "max"}
    kb = new_session._build_keyboard(sel)
    flat = [btn.text for row in kb.inline_keyboard for btn in row]
    # Selected project marked with the green dot.
    assert any("🟢" in t and "Project B" in t for t in flat)
    assert any("📁" in t and "Project A" in t for t in flat)
    # Selected model + effort marked.
    assert any(t.startswith("● ") and "1M" in t for t in flat)
    assert any(t == "● Max" for t in flat)
    # Start + Cancel present.
    assert any("Start" in t for t in flat)
    assert any(t == "Cancel" for t in flat)


def test_build_keyboard_callback_data(projects_toml) -> None:
    sel = new_session._default_selection()
    kb = new_session._build_keyboard(sel)
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "ccgrampro:new:project:0" in datas
    assert "ccgrampro:new:project:1" in datas
    assert "ccgrampro:new:model:opus48-1m" in datas
    assert "ccgrampro:new:effort:max" in datas
    assert "ccgrampro:new:start" in datas
    assert "ccgrampro:new:cancel" in datas


def test_render_text_shows_selection(projects_toml) -> None:
    sel = {"project": 0, "model": "opus48-1m", "effort": "high"}
    text = new_session._render_text(sel)
    assert "Project A" in text
    assert "Opus 4.8 · 1M" in text
    assert "High" in text


def test_get_selection_initializes_when_missing() -> None:
    ud: dict = {}
    sel = new_session._get_selection(ud)
    assert sel == new_session._default_selection()
    assert ud[new_session._SEL_KEY] is sel


def test_get_selection_handles_none() -> None:
    assert new_session._get_selection(None) == new_session._default_selection()

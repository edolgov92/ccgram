from __future__ import annotations

import pytest
from ccgram_pro.plan_mode import approval_surface, mode_control


@pytest.fixture(autouse=True)
def _reset():
    approval_surface._reset_for_testing()
    yield
    approval_surface._reset_for_testing()


@pytest.fixture
def _fast(monkeypatch):
    monkeypatch.setattr(mode_control, "_PRESS_DELAY_SECONDS", 0.0)


def _patch_scrape(monkeypatch, labels):
    seq = list(labels)
    calls = {"n": 0}

    async def stub(self, window_id, *, capture_fn=None):  # noqa: ARG001
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return seq[i]

    from ccgram.providers.claude import ClaudeProvider

    monkeypatch.setattr(ClaudeProvider, "scrape_current_mode", stub)


def _patch_send_keys(monkeypatch, *, fail=False):
    sent: list[str] = []

    async def stub(self, window_id, keys, **kwargs):  # noqa: ARG001
        if fail:
            raise RuntimeError("tmux down")
        sent.append(keys)

    from ccgram.tmux_manager import TmuxManager

    monkeypatch.setattr(TmuxManager, "send_keys", stub)
    return sent


async def test_drive_to_plan_already_in_plan_no_presses(monkeypatch, _fast) -> None:
    _patch_scrape(monkeypatch, ["Plan"])
    sent = _patch_send_keys(monkeypatch)
    assert await mode_control.drive_to_mode("@1", "plan") is True
    assert sent == []


async def test_drive_to_plan_from_edit_presses_once(monkeypatch, _fast) -> None:
    _patch_scrape(monkeypatch, ["Edit", "Plan"])
    sent = _patch_send_keys(monkeypatch)
    assert await mode_control.drive_to_mode("@1", "plan") is True
    assert sent == ["BTab"]


async def test_drive_to_coding_from_plan(monkeypatch, _fast) -> None:
    _patch_scrape(monkeypatch, ["Plan", "Edit"])
    sent = _patch_send_keys(monkeypatch)
    assert await mode_control.drive_to_mode("@1", "coding") is True
    assert sent == ["BTab"]


async def test_drive_gives_up_after_bound(monkeypatch, _fast) -> None:
    _patch_scrape(monkeypatch, ["Plan"])  # never leaves plan
    sent = _patch_send_keys(monkeypatch)
    assert await mode_control.drive_to_mode("@1", "coding") is False
    assert len(sent) == mode_control._MAX_PRESSES


async def test_drive_send_failure_returns_false(monkeypatch, _fast) -> None:
    _patch_scrape(monkeypatch, ["Edit"])
    _patch_send_keys(monkeypatch, fail=True)
    assert await mode_control.drive_to_mode("@1", "plan") is False


def test_approval_surface_appends_settings_for_exitplanmode(monkeypatch) -> None:
    from ccgram.handlers.interactive import interactive_ui as iu

    original = iu._build_interactive_keyboard
    approval_surface.install_plan_approval_surface()
    try:
        plan_kb = iu._build_interactive_keyboard("@5", ui_name="ExitPlanMode")
        flat = [b.callback_data for row in plan_kb.inline_keyboard for b in row]
        assert any(d and d.startswith("ccgrampro:set:open:") for d in flat)
        other_kb = iu._build_interactive_keyboard("@5", ui_name="AskUserQuestion")
        flat2 = [b.callback_data for row in other_kb.inline_keyboard for b in row]
        assert not any(d and d.startswith("ccgrampro:set:open:") for d in flat2)
    finally:
        iu._build_interactive_keyboard = original


def test_approval_surface_install_idempotent() -> None:
    from ccgram.handlers.interactive import interactive_ui as iu

    original = iu._build_interactive_keyboard
    approval_surface.install_plan_approval_surface()
    once = iu._build_interactive_keyboard
    approval_surface.install_plan_approval_surface()
    twice = iu._build_interactive_keyboard
    try:
        assert once is twice
        assert once is not original
    finally:
        iu._build_interactive_keyboard = original

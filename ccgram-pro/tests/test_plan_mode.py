"""Tests for ``ccgram_pro.plan_mode.orchestrator``."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from ccgram_pro import state
from ccgram_pro.plan_mode import orchestrator


@pytest.fixture(autouse=True)
def _reset_install_flag():
    orchestrator._reset_for_testing()
    yield
    orchestrator._reset_for_testing()


def test_should_enter_plan_true_for_claude_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("CCGRAM_FORCED_APPROVAL_MODE", "")  # irrelevant
    assert orchestrator._should_enter_plan("claude") is True


def test_should_enter_plan_false_for_non_claude() -> None:
    assert orchestrator._should_enter_plan("codex") is False
    assert orchestrator._should_enter_plan("shell") is False
    assert orchestrator._should_enter_plan("") is False


def test_should_enter_plan_respects_setting_off(monkeypatch, tmp_path) -> None:
    """When settings.toml turns the toggle off, plan-mode entry skips."""
    settings_path = tmp_path / "layer" / "settings.toml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("[defaults]\nplan_mode_on_new_session = false\n")
    assert orchestrator._should_enter_plan("claude") is False


async def test_enter_records_skipped_when_pane_never_ready(monkeypatch) -> None:
    """If scrape_current_mode never returns a label, we record 'skipped'."""
    state.save(state.WindowSidecar(window_id="@x", window_creation_epoch=0.0))

    monkeypatch.setattr(orchestrator, "_MAX_READY_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(orchestrator, "_POLL_INTERVAL_SECONDS", 0.05)

    async def stub_scrape(self, window_id, *, capture_fn=None):  # noqa: ARG001
        return None

    from ccgram.providers.claude import ClaudeProvider

    monkeypatch.setattr(ClaudeProvider, "scrape_current_mode", stub_scrape)
    await orchestrator._enter_plan_mode("@x")
    sc = state.load("@x")
    assert sc is not None
    assert sc.plan_mode == "skipped"


async def test_enter_records_entered_when_already_in_plan(monkeypatch) -> None:
    """A pane already in Plan mode shouldn't be toggled again."""
    state.save(state.WindowSidecar(window_id="@x", window_creation_epoch=0.0))

    async def stub_scrape(self, window_id, *, capture_fn=None):  # noqa: ARG001
        return "Plan"

    from ccgram.providers.claude import ClaudeProvider

    monkeypatch.setattr(ClaudeProvider, "scrape_current_mode", stub_scrape)
    await orchestrator._enter_plan_mode("@x")
    sc = state.load("@x")
    assert sc is not None
    assert sc.plan_mode == "entered"


async def test_enter_sends_btab_and_verifies(monkeypatch) -> None:
    """Happy path: pane shows non-plan label, BTab sent, verify reports Plan."""
    state.save(state.WindowSidecar(window_id="@x", window_creation_epoch=0.0))

    monkeypatch.setattr(orchestrator, "_MAX_READY_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(orchestrator, "_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(orchestrator, "_MODE_VERIFY_DELAY_SECONDS", 0.01)

    scrape_calls: list[str] = []

    async def stub_scrape(self, window_id, *, capture_fn=None):  # noqa: ARG001
        # First scrape returns Edit; subsequent scrapes return Plan.
        scrape_calls.append("call")
        return "Edit" if len(scrape_calls) == 1 else "Plan"

    send_keys_calls: list[tuple[str, str]] = []

    async def stub_send_keys(self, window_id, keys, **kwargs):  # noqa: ARG001
        send_keys_calls.append((window_id, keys))

    from ccgram.providers.claude import ClaudeProvider
    from ccgram.tmux_manager import TmuxManager

    monkeypatch.setattr(ClaudeProvider, "scrape_current_mode", stub_scrape)
    monkeypatch.setattr(TmuxManager, "send_keys", stub_send_keys)

    await orchestrator._enter_plan_mode("@x")
    assert send_keys_calls and send_keys_calls[0][1] == "BTab"
    sc = state.load("@x")
    assert sc is not None
    assert sc.plan_mode == "entered"


async def test_enter_skipped_when_btab_send_fails(monkeypatch) -> None:
    state.save(state.WindowSidecar(window_id="@x", window_creation_epoch=0.0))
    monkeypatch.setattr(orchestrator, "_POLL_INTERVAL_SECONDS", 0.01)

    async def stub_scrape(self, window_id, *, capture_fn=None):  # noqa: ARG001
        return "Edit"

    async def boom(self, window_id, keys, **kwargs):  # noqa: ARG001
        raise RuntimeError("tmux down")

    from ccgram.providers.claude import ClaudeProvider
    from ccgram.tmux_manager import TmuxManager

    monkeypatch.setattr(ClaudeProvider, "scrape_current_mode", stub_scrape)
    monkeypatch.setattr(TmuxManager, "send_keys", boom)

    await orchestrator._enter_plan_mode("@x")
    sc = state.load("@x")
    assert sc is not None
    assert sc.plan_mode == "skipped"


async def test_install_plan_mode_entry_is_idempotent(monkeypatch) -> None:
    """A re-run of extension install must not double-wrap _create_window_and_bind."""
    from ccgram.handlers.topics import directory_callbacks as dc

    original = dc._create_window_and_bind
    orchestrator.install_plan_mode_entry()
    once = dc._create_window_and_bind
    orchestrator.install_plan_mode_entry()
    twice = dc._create_window_and_bind
    assert once is twice
    assert once is not original

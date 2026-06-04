"""Tests for ``ccgram_pro.output_pipeline.progress_bubble``."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from ccgram_pro.output_pipeline import progress_bubble


@pytest.fixture(autouse=True)
def _reset_bubbles():
    progress_bubble._reset_for_testing()
    yield
    progress_bubble._reset_for_testing()


def _make_bot(send_message_result_id: int = 42) -> Any:
    class _Bot:
        def __init__(self) -> None:
            self.send_message = AsyncMock(
                return_value=type("M", (), {"message_id": send_message_result_id})()
            )
            self.edit_message_text = AsyncMock()
            self.delete_message = AsyncMock()

    return _Bot()


def _progress_line(text: str) -> str:
    import json

    block = {
        "type": "text",
        "text": f"<!--ccgram:progress-->{text}<!--/ccgram:progress-->",
    }
    return json.dumps({"type": "assistant", "message": {"content": [block]}})


async def test_start_bubble_posts_message_and_registers() -> None:
    bot = _make_bot()
    await progress_bubble.start_bubble(
        window_id="@1", bot=bot, chat_id=999, thread_id=7
    )
    assert progress_bubble.is_active("@1")
    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 999
    assert kwargs["message_thread_id"] == 7
    assert "Working on your request" in kwargs["text"]


async def test_start_finalizes_existing_before_reposting() -> None:
    bot = _make_bot()
    await progress_bubble.start_bubble(window_id="@1", bot=bot, chat_id=1, thread_id=1)
    # A leaked bubble from a turn that ended without a Stop is finalized + the
    # new turn reposts a fresh spinner (never stacks on a stale one).
    await progress_bubble.start_bubble(window_id="@1", bot=bot, chat_id=1, thread_id=1)
    assert bot.send_message.await_count == 2
    assert progress_bubble.is_active("@1")


async def test_tick_edit_failure_drops_registry_entry(monkeypatch) -> None:
    monkeypatch.setattr(progress_bubble, "_TICK_INTERVAL_SECONDS", 0.01)
    bot = _make_bot()
    bot.edit_message_text = AsyncMock(side_effect=RuntimeError("deleted"))
    await progress_bubble.start_bubble(window_id="@1", bot=bot, chat_id=1, thread_id=1)
    assert progress_bubble.is_active("@1")
    await asyncio.sleep(0.05)
    assert not progress_bubble.is_active("@1")
    await progress_bubble.start_bubble(window_id="@1", bot=bot, chat_id=1, thread_id=1)
    assert bot.send_message.await_count == 2


async def test_start_bubble_isolates_different_windows() -> None:
    bot = _make_bot()
    await progress_bubble.start_bubble(window_id="@a", bot=bot, chat_id=1, thread_id=1)
    await progress_bubble.start_bubble(window_id="@b", bot=bot, chat_id=2, thread_id=2)
    assert progress_bubble.is_active("@a")
    assert progress_bubble.is_active("@b")
    assert bot.send_message.await_count == 2


async def test_finalize_deletes_when_no_bullets() -> None:
    bot = _make_bot()
    await progress_bubble.start_bubble(window_id="@1", bot=bot, chat_id=1, thread_id=1)
    assert progress_bubble.is_active("@1")
    await progress_bubble.finalize_bubble("@1", bot)
    assert not progress_bubble.is_active("@1")
    # No progress notes → no record worth keeping → delete the empty bubble.
    bot.delete_message.assert_awaited_once()
    bot.edit_message_text.assert_not_called()


async def test_finalize_keeps_message_when_bullets() -> None:
    bot = _make_bot()
    await progress_bubble.start_bubble(window_id="@1", bot=bot, chat_id=1, thread_id=1)
    progress_bubble._bubbles["@1"].bullets.append("Read auth.py")
    await progress_bubble.finalize_bubble("@1", bot)
    bot.delete_message.assert_not_called()
    final_text = bot.edit_message_text.await_args.kwargs["text"]
    assert "Completed" in final_text
    assert "Read auth.py" in final_text


async def test_finalize_then_start_posts_fresh() -> None:
    bot = _make_bot()
    await progress_bubble.start_bubble(window_id="@1", bot=bot, chat_id=1, thread_id=1)
    await progress_bubble.finalize_bubble("@1", bot)
    await progress_bubble.start_bubble(window_id="@1", bot=bot, chat_id=1, thread_id=1)
    assert bot.send_message.await_count == 2


async def test_finalize_when_no_active() -> None:
    bot = _make_bot()
    await progress_bubble.finalize_bubble("@nope", bot)
    bot.delete_message.assert_not_called()


async def test_stop_for_interactive_keeps_awaiting_bubble() -> None:
    bot = _make_bot()
    await progress_bubble.start_bubble(window_id="@1", bot=bot, chat_id=1, thread_id=1)
    await progress_bubble.stop_for_interactive("@1", bot)
    assert not progress_bubble.is_active("@1")
    # Question surfaced — KEEP a visible "Awaiting your answer" status even with
    # no progress notes (no summary follows to tell the user the turn ended).
    bot.delete_message.assert_not_called()
    final_text = bot.edit_message_text.await_args.kwargs["text"]
    assert "Awaiting your answer" in final_text


async def test_finalize_keeps_empty_when_flag_set() -> None:
    bot = _make_bot()
    await progress_bubble.start_bubble(window_id="@1", bot=bot, chat_id=1, thread_id=1)
    await progress_bubble.finalize_bubble("@1", bot, keep_when_empty=True)
    bot.delete_message.assert_not_called()
    bot.edit_message_text.assert_awaited()


async def test_begin_for_turn_resolves_chat_from_binding(monkeypatch) -> None:
    import ccgram.thread_router as tr

    monkeypatch.setattr(
        tr,
        "thread_router",
        type("R", (), {"resolve_chat_id": lambda self, u, t: 555})(),
    )
    monkeypatch.setattr(progress_bubble, "_resolve_transcript", lambda wid: None)
    monkeypatch.setattr(
        "ccgram_pro.input_pipeline.silencer_guard.is_silent_for_window",
        lambda wid: True,
    )
    bot = _make_bot()
    await progress_bubble.begin_for_turn(
        window_id="@1", user_id=7, thread_id=2, bot=bot, fallback_chat_id=111
    )
    assert progress_bubble.is_active("@1")
    # Binding (555) wins over the stale-message fallback (111).
    assert bot.send_message.await_args.kwargs["chat_id"] == 555


async def test_begin_for_turn_uses_fallback_when_binding_missing(monkeypatch) -> None:
    import ccgram.thread_router as tr

    monkeypatch.setattr(
        tr, "thread_router", type("R", (), {"resolve_chat_id": lambda self, u, t: 0})()
    )
    monkeypatch.setattr(progress_bubble, "_resolve_transcript", lambda wid: None)
    monkeypatch.setattr(
        "ccgram_pro.input_pipeline.silencer_guard.is_silent_for_window",
        lambda wid: True,
    )
    bot = _make_bot()
    await progress_bubble.begin_for_turn(
        window_id="@1", user_id=7, thread_id=2, bot=bot, fallback_chat_id=111
    )
    assert bot.send_message.await_args.kwargs["chat_id"] == 111


async def test_begin_for_turn_noop_when_not_silent(monkeypatch) -> None:
    monkeypatch.setattr(
        "ccgram_pro.input_pipeline.silencer_guard.is_silent_for_window",
        lambda wid: False,
    )
    bot = _make_bot()
    await progress_bubble.begin_for_turn(
        window_id="@1", user_id=7, thread_id=2, bot=bot, fallback_chat_id=111
    )
    assert not progress_bubble.is_active("@1")
    bot.send_message.assert_not_called()


async def test_sweep_stale_bubbles_deletes_persisted() -> None:
    from ccgram_pro import state

    sc = state.WindowSidecar(window_id="@s", window_creation_epoch=0.0)
    sc.current_progress_bubble = {"thread_id": 5, "message_id": 99, "chat_id": -10}
    state.save(sc)
    bot = _make_bot()
    swept = await progress_bubble.sweep_stale_bubbles(bot)
    assert swept == 1
    assert bot.delete_message.await_args.kwargs["message_id"] == 99
    reloaded = state.load("@s")
    assert reloaded is not None and reloaded.current_progress_bubble is None


async def test_start_swallows_send_failure() -> None:
    bot = _make_bot()
    bot.send_message.side_effect = RuntimeError("network down")
    await progress_bubble.start_bubble(window_id="@1", bot=bot, chat_id=1, thread_id=1)
    assert not progress_bubble.is_active("@1")


async def test_bubble_grows_bullets_from_transcript(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(progress_bubble, "_TICK_INTERVAL_SECONDS", 0.02)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")  # start empty so offset begins at 0
    bot = _make_bot()
    await progress_bubble.start_bubble(
        window_id="@1",
        bot=bot,
        chat_id=1,
        thread_id=1,
        transcript_path=str(transcript),
    )
    transcript.write_text(
        _progress_line("Reading the auth module")
        + "\n"
        + _progress_line("Editing login")
        + "\n"
    )
    await asyncio.sleep(0.08)
    await progress_bubble.finalize_bubble("@1", bot)
    final_text = bot.edit_message_text.await_args.kwargs["text"]
    assert "Reading the auth module" in final_text
    assert "Editing login" in final_text


async def test_bubble_ignores_progress_before_start_offset(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(progress_bubble, "_TICK_INTERVAL_SECONDS", 0.02)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_progress_line("OLD from a prior turn") + "\n")
    bot = _make_bot()
    await progress_bubble.start_bubble(
        window_id="@1",
        bot=bot,
        chat_id=1,
        thread_id=1,
        transcript_path=str(transcript),
    )
    with transcript.open("a") as fh:
        fh.write(_progress_line("NEW this turn") + "\n")
    await asyncio.sleep(0.08)
    await progress_bubble.finalize_bubble("@1", bot)
    final_text = bot.edit_message_text.await_args.kwargs["text"]
    assert "NEW this turn" in final_text
    assert "OLD from a prior turn" not in final_text


def test_render_caps_visible_bullets() -> None:
    bullets = [f"step {i}" for i in range(60)]
    text = progress_bubble._render(progress_bubble._WORKING_HEADER, bullets)
    assert len(text) <= 4096
    assert "earlier steps" in text
    assert "step 59" in text


def test_render_empty_bullets_is_header_only() -> None:
    text = progress_bubble._render(progress_bubble._WORKING_HEADER, [])
    assert text == progress_bubble._WORKING_HEADER


def test_format_elapsed_below_minute() -> None:
    assert progress_bubble._format_elapsed(7.0) == "7s"


def test_format_elapsed_with_minutes() -> None:
    assert progress_bubble._format_elapsed(125.0) == "2m 05s"


def test_format_text_contains_spinner_and_elapsed() -> None:
    started = time.time() - 12.0
    text = progress_bubble._format_text(started, 1)
    assert "Working on your request" in text
    assert "12s" in text
    assert any(ch in text for ch in progress_bubble._SPINNER)

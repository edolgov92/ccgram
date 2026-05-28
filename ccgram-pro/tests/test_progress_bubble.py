"""Tests for ``ccgram_pro.output_pipeline.progress_bubble``."""

from __future__ import annotations

import asyncio
import time
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
    """A Bot stub whose async methods record calls + return predictable ids."""

    class _Bot:
        def __init__(self) -> None:
            self.send_message = AsyncMock(
                return_value=type("M", (), {"message_id": send_message_result_id})()
            )
            self.edit_message_text = AsyncMock()
            self.delete_message = AsyncMock()

    return _Bot()


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
    assert "Working" in kwargs["text"]


async def test_start_bubble_is_idempotent_per_window() -> None:
    bot = _make_bot()
    await progress_bubble.start_bubble(
        window_id="@1", bot=bot, chat_id=1, thread_id=1
    )
    await progress_bubble.start_bubble(
        window_id="@1", bot=bot, chat_id=1, thread_id=1
    )
    # Second start was a no-op — only one send_message call total.
    assert bot.send_message.await_count == 1


async def test_start_bubble_isolates_different_windows() -> None:
    bot = _make_bot()
    await progress_bubble.start_bubble(
        window_id="@a", bot=bot, chat_id=1, thread_id=1
    )
    await progress_bubble.start_bubble(
        window_id="@b", bot=bot, chat_id=2, thread_id=2
    )
    assert progress_bubble.is_active("@a")
    assert progress_bubble.is_active("@b")
    assert bot.send_message.await_count == 2


async def test_stop_bubble_cancels_task_and_deletes_message() -> None:
    bot = _make_bot()
    await progress_bubble.start_bubble(
        window_id="@1", bot=bot, chat_id=1, thread_id=1
    )
    assert progress_bubble.is_active("@1")
    await progress_bubble.stop_bubble("@1", bot)
    assert not progress_bubble.is_active("@1")
    bot.delete_message.assert_awaited_once()


async def test_stop_bubble_when_no_active() -> None:
    bot = _make_bot()
    # Must not raise even when nothing is registered.
    await progress_bubble.stop_bubble("@nope", bot)
    bot.delete_message.assert_not_called()


async def test_start_swallows_send_failure() -> None:
    bot = _make_bot()
    bot.send_message.side_effect = RuntimeError("network down")
    # Must not raise — the bubble is best-effort UI.
    await progress_bubble.start_bubble(
        window_id="@1", bot=bot, chat_id=1, thread_id=1
    )
    assert not progress_bubble.is_active("@1")


async def test_tick_loop_edits_periodically(monkeypatch) -> None:
    """Patch the tick interval down so the test runs in milliseconds."""
    monkeypatch.setattr(progress_bubble, "_TICK_INTERVAL_SECONDS", 0.05)
    bot = _make_bot()
    await progress_bubble.start_bubble(
        window_id="@1", bot=bot, chat_id=1, thread_id=1
    )
    await asyncio.sleep(0.15)  # at least 2 ticks should fire
    assert bot.edit_message_text.await_count >= 2
    await progress_bubble.stop_bubble("@1", bot)


def test_format_elapsed_below_minute() -> None:
    assert progress_bubble._format_elapsed(7.0) == "7s"


def test_format_elapsed_with_minutes() -> None:
    assert progress_bubble._format_elapsed(125.0) == "2m 05s"


def test_format_text_contains_spinner_and_elapsed() -> None:
    started = time.time() - 12.0
    text = progress_bubble._format_text(started, 1)
    assert "Working" in text
    assert "12s" in text
    assert any(ch in text for ch in progress_bubble._SPINNER)

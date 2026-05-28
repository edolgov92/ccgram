"""Live "🔧 Working…" bubble that edits in place during a Claude turn.

When the user sends a message in a silent-mode topic, this module posts
one Telegram message ("🔧 Working… (15s)") and starts a per-window
asyncio task that edits the message every ~5 seconds with the elapsed
time and a rotating spinner glyph. On Stop the bubble is *removed* —
the layer's summary message (with the View-full link) replaces it.

The bubble is intentionally minimal: no inline keyboard, no per-tick
tool-call count (would require live transcript scanning), no per-window
emoji churn. Just a single calm "Claude is working, here's how long"
indicator that fixes the user's complaint of "I send a message and have
no idea if anything is happening".
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger()


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_TICK_INTERVAL_SECONDS = 5.0


@dataclass
class _ActiveBubble:
    chat_id: int
    thread_id: int
    message_id: int
    task: asyncio.Task[None]
    started_at: float


# Per-window active bubble. Keyed by window_id so concurrent topics don't
# share bubbles and so cancel-on-Stop maps cleanly from event.window_key.
_bubbles: dict[str, _ActiveBubble] = {}


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs:02d}s"


def _format_text(started_at: float, tick: int) -> str:
    elapsed = time.time() - started_at
    spinner = _SPINNER[tick % len(_SPINNER)]
    return f"{spinner} Working… ({_format_elapsed(elapsed)})"


async def _tick_loop(window_id: str, bot: Any) -> None:
    """Periodically edit the bubble until cancelled by ``stop_bubble``."""
    bubble = _bubbles.get(window_id)
    if bubble is None:
        return
    tick = 1
    try:
        while True:
            await asyncio.sleep(_TICK_INTERVAL_SECONDS)
            tick += 1
            text = _format_text(bubble.started_at, tick)
            try:
                await bot.edit_message_text(
                    chat_id=bubble.chat_id,
                    message_id=bubble.message_id,
                    text=text,
                )
            except Exception:  # noqa: BLE001 -- bubble must never crash the layer
                logger.debug(
                    "progress bubble edit failed for %s", window_id, exc_info=True
                )
                # If editing keeps failing (e.g. user deleted the message)
                # we stop trying — the user can still see their reply.
                return
    except asyncio.CancelledError:
        return


async def start_bubble(
    *,
    window_id: str,
    bot: Any,
    chat_id: int,
    thread_id: int,
) -> None:
    """Post a fresh bubble for *window_id* and start the tick loop.

    A no-op when a bubble is already running for the window (e.g.
    rapid-fire batched flushes). This keeps the chat clean even if the
    user double-taps Send.
    """
    if window_id in _bubbles:
        return
    started_at = time.time()
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=_format_text(started_at, 0),
            disable_notification=True,
        )
    except Exception:  # noqa: BLE001 -- never abort the send-flow because of UI
        logger.warning("could not post progress bubble", exc_info=True)
        return
    task = asyncio.create_task(_tick_loop(window_id, bot))
    _bubbles[window_id] = _ActiveBubble(
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=sent.message_id,
        task=task,
        started_at=started_at,
    )


async def stop_bubble(window_id: str, bot: Any) -> None:
    """Cancel the tick loop and delete the bubble message.

    Called from the Stop summarizer right before it posts the layer's
    final summary message, so the user sees one clean transition from
    "🔧 Working…" → summary, not a stack.
    """
    bubble = _bubbles.pop(window_id, None)
    if bubble is None:
        return
    bubble.task.cancel()
    try:
        await bubble.task
    except (asyncio.CancelledError, BaseException):  # noqa: BLE001 -- tolerate any exit reason
        pass
    try:
        await bot.delete_message(chat_id=bubble.chat_id, message_id=bubble.message_id)
    except Exception:  # noqa: BLE001 -- if the delete fails it's fine, summary replaces it
        logger.debug("bubble delete failed for %s", window_id, exc_info=True)


def is_active(window_id: str) -> bool:
    """Return True when a bubble is currently running for *window_id*."""
    return window_id in _bubbles


def _reset_for_testing() -> None:
    """Cancel every running bubble + drop the registry. Test-harness only."""
    for bubble in _bubbles.values():
        bubble.task.cancel()
    _bubbles.clear()

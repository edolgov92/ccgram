"""PTB callback handlers for the batch ``Send`` / ``Clear`` inline buttons.

Callback data shape: ``ccgrampro:batch:{action}:{window_id}`` where
*action* is ``flush`` or ``clear``. The action invokes :mod:`batcher`
and then edits the status message to a stable final form so the user
sees an unambiguous "sent" / "cleared" confirmation.
"""

from __future__ import annotations

from typing import Any

import structlog

from . import intercept
from .batcher import clear, flush

logger = structlog.get_logger()


def _parse_callback(data: str) -> tuple[str, str] | None:
    """Parse ``ccgrampro:batch:<action>:<window_id>``."""
    parts = data.split(":")
    if len(parts) < 4 or parts[0] != "ccgrampro" or parts[1] != "batch":
        return None
    action = parts[2]
    window_id = ":".join(parts[3:])  # foreign-window ids contain a colon
    if action not in ("flush", "clear"):
        return None
    return action, window_id


async def handle_batch_callback(update: Any, context: Any) -> None:
    """Single entry-point for both Send and Clear buttons."""
    query = update.callback_query
    if query is None or not query.data:
        return
    parsed = _parse_callback(query.data)
    if parsed is None:
        await query.answer("Invalid callback", show_alert=True)
        return
    action, window_id = parsed

    user_id = query.from_user.id if query.from_user else 0
    thread_id = query.message.message_thread_id if query.message else None
    if thread_id is None:
        await query.answer("No topic context", show_alert=True)
        return

    if action == "flush":
        await _do_flush(query, user_id, thread_id, window_id, context)
    else:
        await _do_clear(query, user_id, thread_id)


async def _do_flush(
    query: Any, user_id: int, thread_id: int, window_id: str, context: Any
) -> None:
    """Run the batcher flush, forward the combined text, edit status to ✅."""
    from ccgram.handlers.text.text_handler import _forward_message
    from ccgram.telegram_client import PTBTelegramClient

    result = await flush(window_id)
    if result is None:
        await query.answer("Nothing to send")
        return

    # Call the ORIGINAL forward (captured in intercept) so we don't loop
    # back through the batching wrapper.
    original = intercept._ORIGINAL_FORWARD_MESSAGE
    if original is None:
        await query.answer("Internal error: forward not wired", show_alert=True)
        return

    client = PTBTelegramClient(context.bot)
    try:
        await original(
            window_id,
            user_id,
            thread_id,
            result.combined_text,
            client,
            query.message,  # the status message, used as the reply anchor
        )
    except Exception:  # noqa: BLE001 -- surface a useful toast no matter what
        logger.exception("batch flush failed for window %s", window_id)
        await query.answer("Send failed — see logs", show_alert=True)
        return

    suffix = " (with preamble)" if result.preamble_included else ""
    await _finalise(
        query,
        user_id,
        thread_id,
        f"✅ Sent {result.item_count} item{'s' if result.item_count != 1 else ''}{suffix}",
    )
    await query.answer("Sent")

    # Optionally kick off the live "🔧 Working…" bubble. Off by default —
    # the ack reaction is the heartbeat; the bubble reads as spam.
    from ..config import load_settings
    from ..output_pipeline import progress_bubble

    chat_id = query.message.chat.id if query.message else None
    if chat_id is not None and load_settings().defaults.progress_bubble:
        await progress_bubble.start_bubble(
            window_id=window_id,
            bot=context.bot,
            chat_id=chat_id,
            thread_id=thread_id,
        )


async def _do_clear(query: Any, user_id: int, thread_id: int) -> None:
    """Drop the buffered items, edit status to 🗑️ Cleared."""
    parsed = _parse_callback(query.data) if query.data else None
    if parsed is None:
        await query.answer("Invalid callback", show_alert=True)
        return
    _action, window_id = parsed
    removed = await clear(window_id)
    if removed == 0:
        await query.answer("Nothing to clear")
        return
    await _finalise(
        query,
        user_id,
        thread_id,
        f"🗑️ Cleared {removed} item{'s' if removed != 1 else ''}",
    )
    await query.answer("Cleared")


async def _finalise(query: Any, user_id: int, thread_id: int, text: str) -> None:
    """Edit the status reply to *text* and forget our tracked message id."""
    from telegram.error import TelegramError

    msg_id = intercept.clear_status_message(user_id, thread_id)
    if msg_id is None:
        return
    try:
        await query.message.edit_text(text=text, reply_markup=None)
    except TelegramError as exc:
        logger.debug("finalise edit failed: %s", exc)

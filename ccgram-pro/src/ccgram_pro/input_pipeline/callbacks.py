"""PTB callback handlers for the batch ``Send`` / ``Clear`` inline buttons.

Callback data shape: ``ccgrampro:batch:{action}:{window_id}`` where
*action* is ``flush`` or ``clear``. The action invokes :mod:`batcher` and
then DELETES the status message so the conversation stays clean — no
lingering "sent" / "cleared" notification. Feedback (when any) is an
ephemeral callback toast, never a chat message. Delivery failures are the
one exception: those keep the status message (with its buttons) and surface
the error as an alert toast so the user can retry.
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
    """Send/Clear button entry-point, then stop further handlers.

    Registered before ccgram's catch-all CallbackQueryHandler so this
    runs first; ``ApplicationHandlerStop`` prevents the catch-all from
    also processing the same callback.
    """
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import ApplicationHandlerStop

    await _dispatch_batch(update, context)
    raise ApplicationHandlerStop


async def _dispatch_batch(update: Any, context: Any) -> None:
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
    """Flush the batch, forward the combined text, then delete the status msg."""
    # Lazy: ccgram internal — deferred to avoid an import cycle with ccgram bootstrap (the layer is imported during bootstrap).
    from ccgram.telegram_client import PTBTelegramClient

    result = await flush(window_id)
    if result is None:
        # Empty batch (e.g. double-tap) — just remove the status message.
        await _delete_status(query, user_id, thread_id)
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
        # Lazy: contextlib only needed on this error path.
        import contextlib

        # Lazy: PTB error type only needed on this error path.
        from telegram.error import TelegramError

        with contextlib.suppress(TelegramError):
            await query.answer("Send failed — see logs", show_alert=True)
        return

    # Kick off the live "⚙️ Working on your request…" bubble FIRST — before the
    # status-message cleanup — so a stale callback (an expired query.answer in
    # _delete_status) can never abort the flow before the bubble is posted. This
    # is exactly the regression that silently dropped the "processing" status:
    # the batch sat a few minutes, the callback expired, _delete_status raised,
    # and begin_for_turn never ran. chat_id is resolved from the topic binding
    # inside begin_for_turn (never the stale query), so it doesn't need the query.
    # Lazy: deferred to avoid a heavy/cyclic import at module load.
    from ..output_pipeline import progress_bubble

    fallback_chat_id = query.message.chat.id if query.message else None
    await progress_bubble.begin_for_turn(
        window_id=window_id,
        user_id=user_id,
        thread_id=thread_id,
        bot=context.bot,
        fallback_chat_id=fallback_chat_id,
    )

    # Keep the chat clean: delete the batch status message rather than leaving
    # a "Sent N items" notification. The user's own messages keep their read
    # reaction; Claude's reply follows. Best-effort — never raises (see below).
    await _delete_status(query, user_id, thread_id)


async def _do_clear(query: Any, user_id: int, thread_id: int) -> None:
    """Drop the buffered items and delete the status message (toast feedback)."""
    parsed = _parse_callback(query.data) if query.data else None
    if parsed is None:
        await query.answer("Invalid callback", show_alert=True)
        return
    _action, window_id = parsed
    removed = await clear(window_id)
    # Delete the status message (clean chat); feedback is an ephemeral toast.
    await _delete_status(
        query,
        user_id,
        thread_id,
        toast=f"🗑️ Cleared {removed} item{'s' if removed != 1 else ''}"
        if removed
        else "Nothing to clear",
    )


async def _delete_status(
    query: Any, user_id: int, thread_id: int, *, toast: str = ""
) -> None:
    """Delete the batch status message + forget it — keeps the chat clean.

    Used on a successful Send (and on Clear) so no "Sent N items" notification
    lingers in the conversation. The user's own batch messages keep their read
    reaction, and Claude's reply follows. Any feedback is an ephemeral callback
    toast, never a chat message.
    """
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.error import TelegramError

    # Lazy: contextlib only needed in this branch.
    import contextlib

    intercept.clear_status_message(user_id, thread_id)  # forget tracked id
    with contextlib.suppress(TelegramError):
        await query.message.delete()
    # query.answer can raise "query is too old" when the batch sat a while before
    # Send — suppress it; this is best-effort cleanup and must never abort the
    # caller (it used to skip the progress bubble that runs after it).
    with contextlib.suppress(TelegramError):
        await query.answer(toast)

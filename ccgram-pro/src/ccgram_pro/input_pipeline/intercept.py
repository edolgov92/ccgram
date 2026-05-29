"""Patches text and voice forward paths to route through the batcher.

We wrap:

- ``ccgram.handlers.text.text_handler._forward_message`` — the text path.
- ``ccgram.handlers.voice.voice_callbacks._handle_send`` — the voice
  approval-flow send.

Both check whether the bound window has ``batch_mode = True`` on its
sidecar. If yes, the message body is queued on the sidecar's
``current_batch`` and a layer-owned status reply with a "📝 Send N
item(s)" inline button is sent / edited in place. If batch_mode is off
(or the window is unbound), the call falls through to the original
handler unchanged.

The "Send" button callback lives in :mod:`callbacks` so the PTB
``CallbackQueryHandler`` registration is colocated with the handler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from .. import state

if TYPE_CHECKING:
    from telegram.ext import Application

logger = structlog.get_logger()

# Single-shot guard so re-runs of extension.install() don't double-wrap.
_installed = False

# Per-(user_id, thread_id) message id of the "📝 Send N item(s)" status
# reply, so the bot edits a single message instead of stacking new ones
# on every batch enqueue.
_status_messages: dict[tuple[int, int], int] = {}


def _is_batched(window_id: str) -> bool:
    """Return True when the owning window has batch_mode on."""
    sidecar = state.load(window_id)
    if sidecar is None:
        # Default is "on" for newly-created sessions per settings.toml,
        # but for unbound windows we keep the legacy direct forward.
        return False
    return sidecar.batch_mode


def _status_text(count: int) -> str:
    return f"📝 {count} message{'s' if count != 1 else ''} batched — tap **Send** to forward."


def _status_keyboard(window_id: str) -> Any:
    """Build the inline keyboard with Send + Clear buttons."""
    # Lazy: PTB types are only needed inside the actual handler path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ Send", callback_data=f"ccgrampro:batch:flush:{window_id}"
                ),
                InlineKeyboardButton(
                    "🗑️ Clear", callback_data=f"ccgrampro:batch:clear:{window_id}"
                ),
            ]
        ]
    )


async def _edit_or_send_status(
    *,
    bot: Any,
    chat_id: int,
    thread_id: int,
    user_id: int,
    window_id: str,
    count: int,
) -> None:
    """Send (or in-place edit) the persistent batch-status reply.

    Lives on this layer's :data:`_status_messages` dict — independent of
    ccgram's status bubble (which is silenced) so we never collide.
    """
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.constants import ParseMode

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.error import TelegramError

    key = (user_id, thread_id)
    msg_id = _status_messages.get(key)
    text = _status_text(count)
    keyboard = _status_keyboard(window_id)
    if msg_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        except TelegramError as exc:
            logger.debug("status edit fell through to fresh send: %s", exc)
            _status_messages.pop(key, None)
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN,
            disable_notification=True,
        )
        _status_messages[key] = sent.message_id
    except TelegramError as exc:
        logger.warning("could not post batch status: %s", exc)


def clear_status_message(user_id: int, thread_id: int) -> int | None:
    """Pop and return the persistent status message id for that topic.

    Returned message id is what the callback should edit to "✅ Sent" /
    "🗑️ Cleared" after a flush or clear action.
    """
    return _status_messages.pop((user_id, thread_id), None)


# ── Patches ────────────────────────────────────────────────────────────


async def _wrapped_forward_message(
    window_id: str,
    user_id: int,
    thread_id: int,
    text: str,
    client: Any,
    message: Any,
) -> None:
    """Replacement for ``text_handler._forward_message``.

    Falls through to the original on non-batched windows so the upstream
    behaviour (typing action, ack reaction, command history) stays
    intact. After a direct (non-batched) forward, the progress bubble
    is started so the user sees "🔧 Working…" until Claude completes.
    """
    if not _is_batched(window_id):
        await _ORIGINAL_FORWARD_MESSAGE(
            window_id, user_id, thread_id, text, client, message
        )
        # Lazy: progress_bubble is part of the output pipeline; deferring
        # the import keeps the input package free of an extra cross-edge.
        from ..config import load_settings

        # Lazy: deferred to avoid a heavy/cyclic import at module load.
        from ..output_pipeline import progress_bubble

        # Lazy: deferred to avoid a heavy/cyclic import at module load.
        from .silencer_guard import is_silent_for_window

        if load_settings().defaults.progress_bubble and is_silent_for_window(window_id):
            bot = message.get_bot() if hasattr(message, "get_bot") else None
            if bot is not None:
                await progress_bubble.start_bubble(
                    window_id=window_id,
                    bot=bot,
                    chat_id=message.chat.id,
                    thread_id=thread_id,
                )
        return
    # Lazy: ack_reaction is exported from message_sender, not reactions
    # (the helper composes config.ack_reaction with set_message_reaction).
    from ccgram.handlers.messaging_pipeline.message_sender import ack_reaction

    await ack_reaction(client, message.chat.id, message.message_id)
    total, _idx = await __import__(
        "ccgram_pro.input_pipeline.batcher", fromlist=["enqueue"]
    ).enqueue(window_id, kind="text", body=text)
    bot = message.get_bot() if hasattr(message, "get_bot") else None
    if bot is None:
        return
    await _edit_or_send_status(
        bot=bot,
        chat_id=message.chat.id,
        thread_id=thread_id,
        user_id=user_id,
        window_id=window_id,
        count=total,
    )


async def _wrapped_voice_send(
    msg: Any,
    query: Any,
    user_id: int,
    message_id: int,
    update: Any,
    context: Any,
) -> None:
    """Replacement for ``voice_callbacks._handle_send``.

    Resolves the bound window and, when batch_mode is on, appends the
    transcribed text to the batch (no immediate ``send_to_window``).
    """
    # Lazy: ccgram internal — deferred to avoid an import cycle with ccgram bootstrap (the layer is imported during bootstrap).
    from ccgram.handlers.callback_helpers import get_thread_id

    # Lazy: ccgram internal — deferred to avoid an import cycle with ccgram bootstrap (the layer is imported during bootstrap).
    from ccgram.thread_router import thread_router

    thread_id = get_thread_id(update)
    if thread_id is None:
        await _ORIGINAL_VOICE_SEND(msg, query, user_id, message_id, update, context)
        return
    window_id = thread_router.resolve_window_for_thread(user_id, thread_id)
    if not window_id or not _is_batched(window_id):
        await _ORIGINAL_VOICE_SEND(msg, query, user_id, message_id, update, context)
        return

    # Lazy: per-user voice pending store lives in ccgram's user_data.
    from ccgram.handlers.user_state import VOICE_PENDING

    pending_store = (
        context.user_data.get(VOICE_PENDING, {}) if context.user_data else {}
    )
    pending_text = pending_store.pop((msg.chat.id, message_id), None)
    if pending_text is None:
        await query.answer("⚠️ Session expired, resend voice message", show_alert=True)
        return

    total, _ = await __import__(
        "ccgram_pro.input_pipeline.batcher", fromlist=["enqueue"]
    ).enqueue(window_id, kind="voice", body=pending_text)
    bot = msg.get_bot()
    await _edit_or_send_status(
        bot=bot,
        chat_id=msg.chat.id,
        thread_id=thread_id,
        user_id=user_id,
        window_id=window_id,
        count=total,
    )
    await query.answer("Batched")


# Captured once during install_input_pipeline().
_ORIGINAL_FORWARD_MESSAGE: Any = None
_ORIGINAL_VOICE_SEND: Any = None


def install_input_pipeline(application: "Application") -> None:
    """Patch the text + voice forward paths and register the Send / Clear
    callback handler on *application*.
    """
    global _installed, _ORIGINAL_FORWARD_MESSAGE, _ORIGINAL_VOICE_SEND
    if _installed:
        return

    # Lazy: text_handler / voice_callbacks pull in a lot of PTB plumbing.
    from ccgram.handlers.text import text_handler as text_handler_mod

    # Lazy: ccgram internal — deferred to avoid an import cycle with ccgram bootstrap (the layer is imported during bootstrap).
    from ccgram.handlers.voice import voice_callbacks as voice_callbacks_mod

    _ORIGINAL_FORWARD_MESSAGE = text_handler_mod._forward_message
    _ORIGINAL_VOICE_SEND = voice_callbacks_mod._handle_send

    text_handler_mod._forward_message = _wrapped_forward_message  # type: ignore[assignment]
    voice_callbacks_mod._handle_send = _wrapped_voice_send  # type: ignore[assignment]

    # Register the Send / Clear callback handler.
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import CallbackQueryHandler

    # Lazy: deferred to avoid a heavy/cyclic import at module load.
    from .callbacks import handle_batch_callback

    application.add_handler(
        CallbackQueryHandler(handle_batch_callback, pattern=r"^ccgrampro:batch:")
    )

    _installed = True
    logger.info("ccgram-pro input pipeline installed — batched text + voice flow")


def _reset_for_testing() -> None:
    """Drop the install guard so a second install can re-wire references."""
    global _installed
    _installed = False
    _status_messages.clear()

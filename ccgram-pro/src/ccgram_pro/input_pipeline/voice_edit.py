"""Voice confirm-card: rename the confirm button + add an inline Edit flow.

ccgram's voice confirm card offered "✓ Send to agent" / "✗ Discard". Under the
layer that button doesn't send to the agent — it APPENDS the transcript to the
batch — so we relabel it "➕ Add to batch" and add an "✏️ Edit" button that lets
the user correct the (LLM-cleaned, but still fallible) transcript before
appending.

The keyboard is patched onto ``voice_handler._build_voice_keyboard`` (mirrors
``plan_mode.approval_surface``). Edit is a free-text step: tapping ✏️ arms a
per-user ``AWAITING_VOICE_EDIT`` flag (layer-local — we never touch ccgram's
``user_state`` keys); a high-priority text handler (group −11, before ccgram's
text handler) consumes the next message in that topic, swaps the stored pending
text, re-renders the card, and stops propagation so the edit never reaches the
agent.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

# Layer-local user_data key holding the pending edit context:
# {"chat_id": int, "voice_msg_id": int, "confirm_msg_id": int, "thread_id": int}
AWAITING_VOICE_EDIT = "_ccgrampro_awaiting_voice_edit"

_EDIT_CB_PREFIX = "ccgrampro:ve:"


def build_voice_keyboard(message_id: int) -> Any:
    """Three-button confirm card: Add to batch · Edit · Discard.

    ``vc:send`` / ``vc:drop`` are kept verbatim so ccgram's dispatch (and the
    layer's batched-voice wrapper) still fire; only the Send label changes.
    """
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ Add to batch", callback_data=f"vc:send:{message_id}"
                ),
                InlineKeyboardButton(
                    "✏️ Edit", callback_data=f"{_EDIT_CB_PREFIX}{message_id}"
                ),
                InlineKeyboardButton(
                    "✗ Discard", callback_data=f"vc:drop:{message_id}"
                ),
            ],
        ]
    )


def _pending_store(context: Any) -> dict[Any, str]:
    # Lazy: ccgram internal — deferred to avoid an import cycle at module load.
    from ccgram.handlers.user_state import VOICE_PENDING

    if context.user_data is None:
        return {}
    return context.user_data.get(VOICE_PENDING, {})


async def handle_voice_edit_callback(update: Any, context: Any) -> None:
    """``ccgrampro:ve:<id>`` — arm the edit flag and prompt for corrected text."""
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import ApplicationHandlerStop

    query = update.callback_query
    try:
        if query is None or not query.data:
            return
        try:
            voice_msg_id = int(query.data[len(_EDIT_CB_PREFIX) :])
        except ValueError:
            await query.answer("Invalid request", show_alert=True)
            return
        msg = query.message
        chat_id = msg.chat.id if msg and msg.chat else 0
        pending = _pending_store(context)
        current = pending.get((chat_id, voice_msg_id))
        if current is None:
            await query.answer(
                "This voice card expired — record again.", show_alert=True
            )
            return
        thread_id = getattr(msg, "message_thread_id", None) or 0
        if context.user_data is not None:
            context.user_data[AWAITING_VOICE_EDIT] = {
                "chat_id": chat_id,
                "voice_msg_id": voice_msg_id,
                "confirm_msg_id": msg.message_id,
                "thread_id": thread_id,
            }
        # Lazy: only needed in this branch.
        import contextlib

        # Lazy: PTB error type only needed here.
        from telegram.error import TelegramError

        with contextlib.suppress(TelegramError):
            await query.edit_message_text(
                text=(
                    f"🎤 Transcribed:\n\n{current}\n\n"
                    "✏️ Send the corrected version as a text message."
                ),
                reply_markup=build_voice_keyboard(voice_msg_id),
            )
        await query.answer("Send the corrected text")
    except Exception:  # noqa: BLE001 -- log, but still stop propagation below
        logger.exception("voice edit callback failed")
    finally:
        raise ApplicationHandlerStop


async def consume_voice_edit_reply(update: Any, context: Any) -> None:
    """Group −11 text handler: if an edit is armed for this topic, consume it.

    Pure pass-through when no edit is armed (returns without stopping), so normal
    messages reach ccgram's text handler untouched.
    """
    pend = context.user_data.get(AWAITING_VOICE_EDIT) if context.user_data else None
    if not pend:
        return
    message = update.message
    if message is None or not (message.text and message.text.strip()):
        return

    # Lazy: ccgram internal — deferred to avoid an import cycle at module load.
    from ccgram.handlers.callback_helpers import get_thread_id

    if pend.get("thread_id", 0) != (get_thread_id(update) or 0):
        return  # an edit armed in a different topic — leave it, pass through

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import ApplicationHandlerStop

    new_text = message.text.strip()
    pending = _pending_store(context)
    key = (pend["chat_id"], pend["voice_msg_id"])
    if key in pending:
        pending[key] = new_text
    if context.user_data is not None:
        context.user_data.pop(AWAITING_VOICE_EDIT, None)

    # Lazy: only needed on the consume path.
    import contextlib

    # Lazy: PTB error type only needed here.
    from telegram.error import TelegramError

    with contextlib.suppress(TelegramError):
        await context.bot.edit_message_text(
            chat_id=pend["chat_id"],
            message_id=pend["confirm_msg_id"],
            text=f"🎤 Transcribed (edited):\n\n{new_text}",
            reply_markup=build_voice_keyboard(pend["voice_msg_id"]),
        )
    # Keep the chat clean — drop the user's correction message.
    with contextlib.suppress(TelegramError):
        await message.delete()
    raise ApplicationHandlerStop

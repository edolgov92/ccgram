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
    """Repost the batch-status reply so it always sits below the newest message.

    Rather than editing the existing status in place (which would leave it
    stranded above any messages the user sent since), we send a fresh status
    message and then delete the previous one — so the "📝 N batched — tap Send"
    prompt follows the conversation to the bottom. Ordering matters: we send
    first and only delete the prior on success, so a failed send never orphans
    the batch (the old prompt with its working buttons stays put).

    Serialized per-window via :func:`state.transaction` so two messages arriving
    back-to-back can't each repost and leave one orphaned. Lives on this layer's
    :data:`_status_messages` dict — independent of ccgram's silenced bubble.
    """
    # Lazy: only needed on this path.
    import contextlib

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.constants import ParseMode

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.error import TelegramError

    key = (user_id, thread_id)
    text = _status_text(count)
    keyboard = _status_keyboard(window_id)
    async with state.transaction(window_id):
        prev_id = _status_messages.get(key)
        try:
            sent = await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=True,
            )
        except TelegramError as exc:
            # Keep prev_id so the existing prompt (and its Send button) survives.
            logger.warning("could not post batch status: %s", exc)
            return
        _status_messages[key] = sent.message_id
        if prev_id is not None and prev_id != sent.message_id:
            with contextlib.suppress(TelegramError):
                await bot.delete_message(chat_id=chat_id, message_id=prev_id)


def clear_status_message(user_id: int, thread_id: int) -> int | None:
    """Pop and return the persistent status message id for that topic.

    Returned message id is what the callback should edit to "✅ Sent" /
    "🗑️ Cleared" after a flush or clear action.
    """
    return _status_messages.pop((user_id, thread_id), None)


# ── Patches ────────────────────────────────────────────────────────────


def _resolve_transcript_path(window_id: str) -> str | None:
    """Resolve the window's live transcript path (for progress-bubble tailing).

    Returns ``None`` if the window/store isn't resolvable (e.g. the query layer
    isn't wired yet); the progress bubble simply tails nothing in that case.
    """
    # Lazy: window_query pulls in the query layer.
    from ccgram.window_query import view_window

    try:
        view = view_window(window_id)
    except RuntimeError:
        return None
    if view and view.transcript_path:
        return str(view.transcript_path)
    return None


async def _touch_workspace_activity(window_id: str) -> None:
    """Refresh ``last_activity_at`` for windows backed by a per-session clone."""
    # Lazy: deferred to avoid a heavy/cyclic import at module load.
    from .. import state

    sidecar = state.load(window_id)
    if sidecar is None or not sidecar.workspace_path:
        return
    # Worktrees are not layer-owned, idle-GC'd directories — never stamp them
    # (the idle sweep keys off last_activity_at; leaving it None keeps the sweep
    # away from git-registered worktrees).
    if sidecar.workspace_strategy == "worktree":
        return
    # Lazy: deferred to avoid a heavy/cyclic import at module load.
    from ..workspaces.manager import touch_activity

    await touch_activity(window_id)


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
    behaviour (typing action, command history) stays intact. After a direct
    (non-batched) forward, the progress bubble is started so the user sees
    "⚙️ Working on your request…" growing with Claude's progress notes until
    the turn completes.
    """
    # A user message is the clearest "session is alive" signal — refresh the
    # workspace activity timestamp so the idle GC doesn't reap an actively-used
    # per-session clone. No-op for windows without a workspace (the common case).
    await _touch_workspace_activity(window_id)

    if not _is_batched(window_id):
        await _ORIGINAL_FORWARD_MESSAGE(
            window_id, user_id, thread_id, text, client, message
        )
        # Lazy: progress_bubble is part of the output pipeline; deferring
        # the import keeps the input package free of an extra cross-edge.
        from ..output_pipeline import progress_bubble

        # begin_for_turn does the progress-bubble + silent-mode gating and
        # resolves chat_id from the topic binding; the message chat is only a
        # last-resort fallback (it can be missing/stale).
        bot = message.get_bot() if hasattr(message, "get_bot") else None
        fallback_chat_id = getattr(getattr(message, "chat", None), "id", None)
        await progress_bubble.begin_for_turn(
            window_id=window_id,
            user_id=user_id,
            thread_id=thread_id,
            bot=bot,
            fallback_chat_id=fallback_chat_id,
        )
        return
    # No ack reaction on the user's batched message — the layer keeps the chat
    # free of bot-authored emoji (see silencer reactions suppression). The batch
    # status bubble is the acknowledgement that the message was queued.
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
    # The transcript is now in the batch — strip the card's actions so it reads
    # as a plain "🎤 Transcribed: …" record (the batch status tracks it).
    # Lazy: only needed on this path.
    import contextlib

    # Lazy: PTB error type only needed here.
    from telegram.error import TelegramError

    with contextlib.suppress(TelegramError):
        await query.edit_message_reply_markup(reply_markup=None)
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


async def _wrapped_transcribe_audio(
    message: Any, transcriber: Any, audio_bytes: bytes
) -> Any:
    """Replacement for ``voice_handler._transcribe_audio`` — adds LLM cleanup.

    Runs the raw Whisper result through :func:`voice_cleanup.clean_transcript`
    so IT homophones (modal↔model, messages↔messengers, …) are corrected before
    the user sees the confirm card. Falls back to the raw result on any failure
    (no LLM configured, timeout, error) and preserves the detected language.
    """
    result = await _ORIGINAL_TRANSCRIBE_AUDIO(message, transcriber, audio_bytes)
    if result is None:
        return result
    # Lazy: voice_cleanup pulls the llm client; only needed on the voice path.
    from .voice_cleanup import clean_transcript

    cleaned = await clean_transcript(result.text)
    if cleaned == result.text:
        return result
    # Lazy: ccgram internal — deferred to avoid an import cycle at module load.
    from ccgram.whisper.base import TranscriptionResult

    return TranscriptionResult(text=cleaned, language=result.language)


# Captured once during install_input_pipeline().
_ORIGINAL_FORWARD_MESSAGE: Any = None
_ORIGINAL_VOICE_SEND: Any = None
_ORIGINAL_TRANSCRIBE_AUDIO: Any = None


def install_input_pipeline(application: "Application") -> None:
    """Patch the text + voice forward paths and register the Send / Clear
    callback handler on *application*.
    """
    global _installed, _ORIGINAL_FORWARD_MESSAGE, _ORIGINAL_VOICE_SEND
    global _ORIGINAL_TRANSCRIBE_AUDIO
    if _installed:
        return

    # Lazy: text_handler / voice_callbacks pull in a lot of PTB plumbing.
    from ccgram.handlers.text import text_handler as text_handler_mod

    # Lazy: ccgram internal — deferred to avoid an import cycle with ccgram bootstrap (the layer is imported during bootstrap).
    from ccgram.handlers.voice import voice_callbacks as voice_callbacks_mod

    # Lazy: ccgram internal — deferred to avoid an import cycle with ccgram bootstrap (the layer is imported during bootstrap).
    from ccgram.handlers.voice import voice_handler as voice_handler_mod

    _ORIGINAL_FORWARD_MESSAGE = text_handler_mod._forward_message
    _ORIGINAL_VOICE_SEND = voice_callbacks_mod._handle_send
    _ORIGINAL_TRANSCRIBE_AUDIO = voice_handler_mod._transcribe_audio

    text_handler_mod._forward_message = _wrapped_forward_message  # type: ignore[assignment]
    voice_callbacks_mod._handle_send = _wrapped_voice_send  # type: ignore[assignment]
    # Clean raw transcripts (IT homophones) before the confirm card renders.
    voice_handler_mod._transcribe_audio = _wrapped_transcribe_audio  # type: ignore[assignment]
    # Relabel the confirm button (➕ Add to batch) + add the ✏️ Edit button.
    # Lazy: deferred to avoid a heavy/cyclic import at module load.
    from . import voice_edit

    voice_handler_mod._build_voice_keyboard = voice_edit.build_voice_keyboard  # type: ignore[assignment]

    # Register the Send / Clear + voice-edit callback handlers.
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters

    # Lazy: deferred to avoid a heavy/cyclic import at module load.
    from .callbacks import handle_batch_callback

    # group=-10: run before ccgram's catch-all CallbackQueryHandler (group 0).
    application.add_handler(
        CallbackQueryHandler(handle_batch_callback, pattern=r"^ccgrampro:batch:"),
        group=-10,
    )
    application.add_handler(
        CallbackQueryHandler(
            voice_edit.handle_voice_edit_callback, pattern=r"^ccgrampro:ve:"
        ),
        group=-10,
    )
    # group=-11: consume a voice-edit reply before ccgram's text handler (group 0)
    # forwards it to the agent. No-op pass-through when no edit is armed.
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND, voice_edit.consume_voice_edit_reply
        ),
        group=-11,
    )
    # Batch-aware photo/document handlers (group -14), ahead of ccgram's core
    # group-0 upload handlers — capture uploads into the batch when batched,
    # else pass through to the immediate-upload path.
    # Lazy: deferred to avoid a heavy/cyclic import at module load.
    from . import file_batch

    file_batch.register_file_batch_handlers(application)

    _installed = True
    logger.info("ccgram-pro input pipeline installed — batched text + voice + files")


def _reset_for_testing() -> None:
    """Drop the install guard so a second install can re-wire references."""
    global _installed
    _installed = False
    _status_messages.clear()

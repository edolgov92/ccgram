"""Batch-aware photo / document handling.

ccgram's core registers ``handle_photo_message`` / ``handle_document_message``
directly at group 0, so an uploaded image or file is saved to
``.ccgram-uploads/`` and forwarded to the agent **immediately** — it never
enters the layer's batch, gets no "📝 N batched" confirmation, and its caption
is truncated to 500 chars with newlines collapsed.

This module registers our own PHOTO / Document handlers in a negative group
(ahead of core). When the bound window is in batch mode we:

1. save the file (reusing core's download/validation helpers),
2. enqueue a ``file`` batch item — the agent-facing "I've uploaded …" line plus
   the **full** caption (newlines kept, no 500-char clamp), and
3. surface the same batch-status bubble as text/voice — then stop propagation
   so core's immediate-forward handler never runs.

When the window is unbound or batch mode is off we return without stopping, so
core's original immediate-upload path handles it unchanged.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import structlog

from . import batcher, intercept

if TYPE_CHECKING:
    from telegram.ext import Application

logger = structlog.get_logger()

# Strip control chars but KEEP newlines/tabs — the batch is delivered as one
# literal block (a single Enter submits), so multi-line captions are fine.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_caption(text: str) -> str:
    return _CONTROL_CHAR_RE.sub("", text).strip()


async def _save_and_enqueue(
    message: Any, user_id: int, thread_id: int, window_id: str, *, is_photo: bool
) -> None:
    # Lazy: ccgram internals — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.file_handler import (
        _UPLOAD_DIR,
        _download_and_save,
        _generate_photo_filename,
        _resolve_upload_dir,
        _sanitize_filename,
    )

    # Lazy: entity-safe reply for the error path.
    from ccgram.handlers.messaging_pipeline.message_sender import safe_reply

    _wid, upload_path, error = _resolve_upload_dir(user_id, thread_id)
    if error or not upload_path:
        await safe_reply(message, f"❌ {error or 'No session working directory.'}")
        return

    # Lazy: PTB constant only needed on the handler/send path.
    from telegram.constants import ChatAction

    await message.chat.send_action(ChatAction.TYPING)

    if is_photo:
        photo = message.photo[-1]
        filename = _generate_photo_filename(photo.file_unique_id)
        file_id, file_size, size_label = photo.file_id, photo.file_size, "Photo"
    else:
        doc = message.document
        filename = _sanitize_filename(doc.file_name or "document")
        file_id, file_size, size_label = doc.file_id, doc.file_size, "File"

    saved = await _download_and_save(
        message, upload_path, filename, file_id, file_size, size_label
    )
    if not saved:
        return  # _download_and_save already replied the error

    rel_path = f"{_UPLOAD_DIR}/{saved}"
    if is_photo:
        body = f"I've uploaded an image to {rel_path} — please take a look."
    else:
        body = f"I've uploaded {saved} to {rel_path}"
    caption = _clean_caption(message.caption or "")
    if caption:
        body += f"\n\nUser note: {caption}"

    total, _idx = await batcher.enqueue(window_id, kind="file", body=body)
    bot = message.get_bot()
    await intercept._edit_or_send_status(
        bot=bot,
        chat_id=message.chat.id,
        thread_id=thread_id,
        user_id=user_id,
        window_id=window_id,
        count=total,
    )


async def _handle_file_upload(update: Any, _context: Any, *, is_photo: bool) -> None:
    """Intercept a photo/document; batch it when the window is batched."""
    user = update.effective_user
    message = update.message
    if user is None or message is None:
        return
    if (is_photo and not message.photo) or (not is_photo and not message.document):
        return

    # Lazy: ccgram config — deferred to avoid a bootstrap import cycle.
    from ccgram.config import config

    if not config.is_user_allowed(user.id):
        return  # let core reject unauthorized users

    # Lazy: ccgram internals — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.callback_helpers import get_thread_id

    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.thread_router import thread_router

    thread_id = get_thread_id(update)
    if thread_id is None:
        return
    window_id = thread_router.resolve_window_for_thread(user.id, thread_id)
    if not window_id or not intercept._is_batched(window_id):
        return  # unbound or batch off — core's immediate upload handles it

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import ApplicationHandlerStop

    try:
        await _save_and_enqueue(
            message, user.id, thread_id, window_id, is_photo=is_photo
        )
    except Exception:  # noqa: BLE001 -- log, then stop the handler chain below
        logger.exception("file-batch failed for %s", window_id)
    finally:
        raise ApplicationHandlerStop


async def handle_photo(update: Any, context: Any) -> None:
    await _handle_file_upload(update, context, is_photo=True)


async def handle_document(update: Any, context: Any) -> None:
    await _handle_file_upload(update, context, is_photo=False)


def register_file_batch_handlers(application: "Application") -> None:
    """Register PHOTO / Document handlers ahead of ccgram's core group-0 ones."""
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import MessageHandler, filters

    # group=-14: ahead of core (group 0) so a batched upload is captured first.
    # PHOTO and Document filters never overlap, so both can share the group.
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo), group=-14)
    application.add_handler(
        MessageHandler(filters.Document.ALL, handle_document), group=-14
    )

"""Clean, structured replacement for the screen-scraped AskUserQuestion UI.

Instead of scraping the TUI and offering blind arrow-key buttons, the layer reads
the structured ``AskUserQuestion`` input from the transcript (see
:mod:`interactive_input`) and posts one button per option. A tap drives the TUI
deterministically (see :mod:`interactive_drive`): reset the cursor to the top,
step down to the chosen option, press Enter. Multi-select toggles with Space and
confirms with Enter.

Installation wraps ``hook_events._handle_notification``: for AskUserQuestion we
post the clean keyboard and SUPPRESS the original scraped UI; everything else
(permission prompts, Codex prompts, …) falls through to ccgram unchanged. Any
failure to read/post falls back to the original handler — the prompt must never
be lost. (ExitPlanMode is handled by :mod:`interactive_plan`, wired through the
same callback prefix.)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog

from . import interactive_plan, interactive_state
from .interactive_drive import (
    drive_cancel,
    drive_multi_select,
    drive_single_select,
)
from .interactive_input import read_active_prompt

logger = structlog.get_logger()

_installed = False
_ORIGINAL_HANDLE_NOTIFICATION: Any = None

AQ_PREFIX = "ccgrampro:aq:"
_RETRY_ATTEMPTS = 3
_RETRY_DELAY = 0.25
_MAX_BUTTON_LABEL = 60


@dataclass
class _PendingAsk:
    window_id: str
    question: str
    options: list[str]
    multi_select: bool
    chat_id: int
    message_id: int
    selected: set[int] = field(default_factory=set)


def _qa_record(question: str, answer_line: str) -> str:
    """A permanent Q&A record kept in the chat history (question + answer)."""
    q = question.strip() or "Claude asked a question"
    return f"❓ {q}\n\n{answer_line}"


# (user_id, thread_id) -> the live AskUserQuestion prompt awaiting a tap.
_pending_asks: dict[tuple[int, int], _PendingAsk] = {}


def _question_keyboard(
    options: list[str], multi_select: bool, selected: set[int]
) -> Any:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows: list[list[InlineKeyboardButton]] = []
    for idx, label in enumerate(options):
        if multi_select:
            mark = "✅ " if idx in selected else "▫️ "
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{mark}{label}"[:_MAX_BUTTON_LABEL],
                        callback_data=f"{AQ_PREFIX}t:{idx}",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        label[:_MAX_BUTTON_LABEL],
                        callback_data=f"{AQ_PREFIX}p:{idx}",
                    )
                ]
            )
    if multi_select:
        rows.append(
            [
                InlineKeyboardButton("✓ Confirm", callback_data=f"{AQ_PREFIX}c"),
                InlineKeyboardButton("✗ Cancel", callback_data=f"{AQ_PREFIX}x"),
            ]
        )
    return InlineKeyboardMarkup(rows)


async def _post_question(
    *, client: Any, user_id: int, thread_id: int, chat_id: int, window_id: str, q: Any
) -> bool:
    # Lazy: interactive_ui owns the shared interactive-mode flag.
    from ccgram.handlers.interactive.interactive_ui import set_interactive_mode

    header = q.question
    if q.total > 1:
        header = f"{header}\n\n(Question 1 of {q.total} — the rest follow.)"
    keyboard = _question_keyboard(q.options, q.multi_select, set())
    try:
        sent = await client.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=f"❓ {header}",
            reply_markup=keyboard,
            disable_notification=True,
        )
    except Exception:  # noqa: BLE001 -- fall back to the scraped UI on any failure
        logger.warning("failed to post clean AskUserQuestion", exc_info=True)
        return False
    if sent is None:
        return False
    _pending_asks[(user_id, thread_id)] = _PendingAsk(
        window_id=window_id,
        question=q.question,
        options=list(q.options),
        multi_select=q.multi_select,
        chat_id=chat_id,
        message_id=sent.message_id,
    )
    set_interactive_mode(user_id, window_id, thread_id)
    return True


def _resolve_transcript(window_id: str) -> str | None:
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.window_query import view_window

    try:
        view = view_window(window_id)
    except RuntimeError:
        return None
    if view and view.transcript_path:
        return str(view.transcript_path)
    return None


async def _post_ask(
    client: Any, users: list[tuple[int, int, str]], question: Any
) -> set[tuple[int, int]]:
    """Post the question keyboard to each binding. Returns the posted keys."""
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.thread_router import thread_router

    posted_keys: set[tuple[int, int]] = set()
    for user_id, thread_id, win in users:
        chat_id = thread_router.resolve_chat_id(user_id, thread_id)
        if chat_id and await _post_question(
            client=client,
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            window_id=win,
            q=question,
        ):
            posted_keys.add((user_id, thread_id))
    return posted_keys


async def _stop_progress_bubble(client: Any, window_id: str) -> None:
    """Finalize the live progress bubble — Claude is now awaiting user input."""
    bot = getattr(client, "_bot", None)
    if bot is None:
        return
    # Lazy: progress_bubble is a sibling output-pipeline module.
    from . import progress_bubble

    await progress_bubble.stop_for_interactive(window_id, bot)


async def _read_active_with_retry(transcript_path: str) -> tuple[str, object] | None:
    for _attempt in range(_RETRY_ATTEMPTS):
        active = read_active_prompt(transcript_path)
        if active is not None:
            return active
        await asyncio.sleep(_RETRY_DELAY)
    return None


async def _maybe_post_clean(event: Any, client: Any) -> bool:
    """Post the clean AskUserQuestion / plan card. Returns whether handled.

    The kind is detected from the transcript (the hook's tool_name is empty for
    every prompt). Ownership is claimed BEFORE the read so the scraped UI is
    suppressed for the whole detection window — eliminating a double-post race.
    The "Working…" spinner is stopped (Claude is now awaiting input). Any binding
    we don't post a clean card for is released so the scraped UI falls back.
    """
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.hook_events import _resolve_users_for_window_key

    users = _resolve_users_for_window_key(getattr(event, "window_key", "") or "")
    if not users:
        return False
    window_id = users[0][2]
    transcript_path = _resolve_transcript(window_id)
    if not transcript_path:
        return False
    for user_id, thread_id, _win in users:
        interactive_state.claim(user_id, thread_id)
    await _stop_progress_bubble(client, window_id)
    posted_keys: set[tuple[int, int]] = set()
    try:
        active = await _read_active_with_retry(transcript_path)
        if active is None:
            return False
        kind, payload = active
        if kind == "ask":
            posted_keys = await _post_ask(client, users, payload)
        elif kind == "plan":
            posted_keys = await interactive_plan.post_plan(client, users, str(payload))
        return bool(posted_keys)
    finally:
        for user_id, thread_id, _win in users:
            if (user_id, thread_id) not in posted_keys:
                interactive_state.release(user_id, thread_id)


async def _wrapped_handle_notification(event: Any, client: Any) -> None:
    try:
        if await _maybe_post_clean(event, client):
            return
    except Exception:  # noqa: BLE001 -- never lose the prompt
        logger.exception("clean interactive prompt failed; falling back to scraped UI")

    await _ORIGINAL_HANDLE_NOTIFICATION(event, client)


def _clear(user_id: int, thread_id: int) -> None:
    _pending_asks.pop((user_id, thread_id), None)
    interactive_state.release(user_id, thread_id)
    # Lazy: interactive_ui owns the shared interactive-mode flag.
    from ccgram.handlers.interactive.interactive_ui import clear_interactive_mode

    clear_interactive_mode(user_id, thread_id)


async def _handle_aq_callback(
    query: Any, user_id: int, thread_id: int, rest: str
) -> None:
    # Lazy: only needed on the callback path.
    import contextlib

    # Lazy: PTB error type only needed on the callback path.
    from telegram.error import TelegramError

    pending = _pending_asks.get((user_id, thread_id))
    if pending is None:
        await query.answer("This prompt is no longer active.", show_alert=True)
        return

    async def _edit(text: str) -> None:
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(text=text)

    if rest.startswith("p:"):
        idx = int(rest[2:])
        ok = await drive_single_select(pending.window_id, idx)
        label = pending.options[idx] if 0 <= idx < len(pending.options) else "?"
        # Keep a permanent Q&A record in history (question + chosen answer).
        answer = f"✅ Your answer: {label}" if ok else "⚠️ Could not reach the agent."
        await _edit(_qa_record(pending.question, answer))
        _clear(user_id, thread_id)
        await query.answer()
    elif rest.startswith("t:"):
        idx = int(rest[2:])
        if idx in pending.selected:
            pending.selected.discard(idx)
        else:
            pending.selected.add(idx)
        with contextlib.suppress(TelegramError):
            await query.edit_message_reply_markup(
                reply_markup=_question_keyboard(
                    pending.options, pending.multi_select, pending.selected
                )
            )
        await query.answer()
    elif rest == "c":
        if not pending.selected:
            await query.answer("Pick at least one option.", show_alert=True)
            return
        ok = await drive_multi_select(pending.window_id, sorted(pending.selected))
        labels = [
            pending.options[i]
            for i in sorted(pending.selected)
            if i < len(pending.options)
        ]
        answer = (
            f"✅ Your answer: {', '.join(labels)}"
            if ok
            else "⚠️ Could not reach the agent."
        )
        await _edit(_qa_record(pending.question, answer))
        _clear(user_id, thread_id)
        await query.answer()
    elif rest == "x":
        await drive_cancel(pending.window_id)
        await _edit(_qa_record(pending.question, "✗ Dismissed (no answer)"))
        _clear(user_id, thread_id)
        await query.answer()
    else:
        await query.answer()


async def handle_interactive_callback(update: Any, _context: Any) -> None:
    """Dispatch ``ccgrampro:aq:*`` / ``ccgrampro:pl:*`` taps, then stop propagation."""
    # Lazy: PTB types only needed on the handler path.
    from telegram.ext import ApplicationHandlerStop

    try:
        query = update.callback_query
        if query is None or not query.data:
            return
        user_id = query.from_user.id if query.from_user else 0
        thread_id = getattr(query.message, "message_thread_id", None) or 0
        data = query.data
        if data.startswith(AQ_PREFIX):
            await _handle_aq_callback(query, user_id, thread_id, data[len(AQ_PREFIX) :])
        elif data.startswith(interactive_plan.PL_PREFIX):
            await interactive_plan.handle_plan_callback(query, user_id, thread_id, data)
    except Exception:  # noqa: BLE001 -- log, but still stop propagation below
        logger.exception("clean interactive callback failed")
    finally:
        raise ApplicationHandlerStop


def install_clean_interactive(application: Any) -> None:
    """Wrap ``_handle_notification`` + register the interactive callback handler."""
    global _installed, _ORIGINAL_HANDLE_NOTIFICATION
    if _installed:
        return
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers import hook_events as hook_events_mod

    # Lazy: PTB types only needed on the handler path.
    from telegram.ext import CallbackQueryHandler

    _ORIGINAL_HANDLE_NOTIFICATION = hook_events_mod._handle_notification
    hook_events_mod._handle_notification = _wrapped_handle_notification  # type: ignore[assignment]
    # Suppress ccgram's scraped interactive UI while the clean keyboard owns a
    # topic (fast path + poll tick + hook fallback all route through it).
    interactive_state.install_interactive_guard()
    application.add_handler(
        CallbackQueryHandler(
            handle_interactive_callback, pattern=r"^ccgrampro:(aq|pl):"
        ),
        group=-10,
    )
    _installed = True
    logger.info(
        "ccgram-pro clean interactive UI installed — structured AskUserQuestion + plan"
    )


def _reset_for_testing() -> None:
    global _installed
    _installed = False
    _pending_asks.clear()
    interactive_state._reset_for_testing()
    interactive_plan._reset_for_testing()

"""Clean ExitPlanMode prompt: short idea + Approve/Keep-planning + full-plan link.

When Claude calls ``ExitPlanMode`` we read the plan markdown from the transcript,
condense it to a 2-3 sentence main idea (see :mod:`plan_summary`), store the full
markdown as a ``plan`` share record (rendered on the web ``/plan`` page), and post
a clean card to Telegram with:

  ✅ Approve · 📝 Keep planning · 📄 View full plan · ⚙️ Settings

Approve drives the native selector to its first option ("Yes, proceed") and
presses Enter; Keep planning sends Escape (dismisses the prompt, stays in plan
mode). The deterministic driving lives in :mod:`interactive_drive`.

NOTE: the approve-option ordering is the Claude TUI built-in (not in the tool
input), so we drive by position — verify against a live pane if Claude's prompt
layout changes.
"""

from __future__ import annotations

from typing import Any

import structlog

from . import interactive_state
from ..share.links import make_plan_url
from ..share.store import save_share
from .interactive_drive import drive_cancel, drive_single_select
from .plan_summary import condense_plan

logger = structlog.get_logger()

PL_PREFIX = "ccgrampro:pl:"
_RETRY_ATTEMPTS = 3
_RETRY_DELAY = 0.25

# (user_id, thread_id) -> (window_id, condensed idea) of the live plan prompt.
_pending_plans: dict[tuple[int, int], tuple[str, str]] = {}


def _plan_record(idea: str, decision: str) -> str:
    """A permanent record kept in history (the plan idea + the user's decision)."""
    body = idea.strip() or "Claude proposed a plan"
    return f"📋 {body}\n\n{decision}"


def _plan_keyboard(*, plan_url: str | None, window_id: str) -> Any:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Lazy: settings_panel installs alongside; import at send time.
    from ..settings_panel import button_for_window

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"{PL_PREFIX}a"),
            InlineKeyboardButton("📝 Keep planning", callback_data=f"{PL_PREFIX}k"),
        ]
    ]
    last_row = [button_for_window(window_id)]
    if plan_url:
        last_row.insert(0, InlineKeyboardButton("📄 View full plan", url=plan_url))
    rows.append(last_row)
    return InlineKeyboardMarkup(rows)


async def post_plan(
    client: Any, users: list[tuple[int, int, str]], plan_md: str
) -> set[tuple[int, int]]:
    """Condense the plan + post the approval card to each binding. Returns posted keys.

    Called by the unified interactive handler (``interactive_clean``), which has
    already claimed ownership + stopped the progress bubble. Condenses + stores
    the share once, then posts per binding.
    """
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.config import config

    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.thread_router import thread_router

    window_id = users[0][2]
    idea = await condense_plan(plan_md)
    share_id = save_share(
        kind="plan",
        title=f"Plan · {window_id}",
        body_markdown=plan_md,
        window_id=window_id,
    )
    plan_url = make_plan_url(bot_token=config.telegram_bot_token, share_id=share_id)

    posted_keys: set[tuple[int, int]] = set()
    for user_id, thread_id, win in users:
        chat_id = thread_router.resolve_chat_id(user_id, thread_id)
        if chat_id and await _post_plan_card(
            client=client,
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            window_id=win,
            idea=idea,
            plan_url=plan_url,
        ):
            posted_keys.add((user_id, thread_id))
    return posted_keys


async def _post_plan_card(
    *,
    client: Any,
    user_id: int,
    thread_id: int,
    chat_id: int,
    window_id: str,
    idea: str,
    plan_url: str | None,
) -> bool:
    # Lazy: interactive_ui owns the shared interactive-mode flag.
    from ccgram.handlers.interactive.interactive_ui import set_interactive_mode

    keyboard = _plan_keyboard(plan_url=plan_url, window_id=window_id)
    try:
        sent = await client.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=f"📋 *Plan ready*\n\n{idea}",
            reply_markup=keyboard,
            disable_notification=True,
        )
    except Exception:  # noqa: BLE001 -- fall back to scraped UI on failure
        logger.warning("failed to post clean plan card", exc_info=True)
        return False
    if sent is None:
        return False
    _pending_plans[(user_id, thread_id)] = (window_id, idea)
    set_interactive_mode(user_id, window_id, thread_id)
    return True


def _clear(user_id: int, thread_id: int) -> None:
    _pending_plans.pop((user_id, thread_id), None)
    interactive_state.release(user_id, thread_id)
    # Lazy: interactive_ui owns the shared interactive-mode flag.
    from ccgram.handlers.interactive.interactive_ui import clear_interactive_mode

    clear_interactive_mode(user_id, thread_id)


async def handle_plan_callback(
    query: Any, user_id: int, thread_id: int, data: str
) -> None:
    """Handle ``ccgrampro:pl:a`` (approve) / ``ccgrampro:pl:k`` (keep planning)."""
    # Lazy: only needed on the callback path.
    import contextlib

    # Lazy: PTB error type only needed on the callback path.
    from telegram.error import TelegramError

    entry = _pending_plans.get((user_id, thread_id))
    if entry is None:
        await query.answer("This plan prompt is no longer active.", show_alert=True)
        return
    window_id, idea = entry

    action = data[len(PL_PREFIX) :]

    async def _edit(text: str) -> None:
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(text=text)

    if action == "a":
        ok = await drive_single_select(window_id, 0)
        decision = (
            "✅ Plan approved — running." if ok else "⚠️ Could not reach the agent."
        )
        await _edit(_plan_record(idea, decision))
        _clear(user_id, thread_id)
        await query.answer()
    elif action == "k":
        ok = await drive_cancel(window_id)
        decision = (
            "📝 Kept planning — refine and try again."
            if ok
            else "⚠️ Could not reach the agent."
        )
        await _edit(_plan_record(idea, decision))
        _clear(user_id, thread_id)
        await query.answer()
    else:
        await query.answer()


def _reset_for_testing() -> None:
    _pending_plans.clear()

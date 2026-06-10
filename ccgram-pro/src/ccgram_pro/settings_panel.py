"""⚙️ Settings panel — change model / reasoning / mode live, mid-session.

A ⚙️ Settings button is attached to the per-turn Stop summary and to the
ExitPlanMode prompt. Tapping it opens an inline menu (a separate reply, so the
summary's View-full / View-diff URL buttons are never clobbered) with radio
rows for Model, Reasoning, and Mode, plus a Git submenu and Close.

Changes apply LIVE to the running session and persist to the sidecar:

- Model    → ``/model <id>``  (non-interactive, preserves the conversation)
- Reasoning→ ``/effort <level>``  (non-interactive)
- Mode     → bounded Shift+Tab drive via :func:`plan_mode.drive_to_mode`

All three levers apply at any time, busy or idle — mirroring Claude Code, where
``/model`` / ``/effort`` (client-side slash commands) and the Shift+Tab mode
cycle are all accepted mid-turn. There is deliberately no idle gate: an earlier
one keyed off a scraped permission-mode marker that a default-mode "Coding"
session never renders, so it misreported every such session as busy and blocked
the change outright.
"""

from __future__ import annotations

from typing import Any

import structlog

from . import state

logger = structlog.get_logger()

_CB_PREFIX = "ccgrampro:set:"
_GIT_MENU_CB = "ccgrampro:git:menu"

_installed = False

# (key, button label, claude --model id)
_MODELS: list[tuple[str, str, str]] = [
    ("fable5", "Fable 5", "claude-fable-5"),
    ("fable5-1m", "Fable 5 · 1M", "claude-fable-5[1m]"),
]
_MODEL_ID = {key: model for key, _label, model in _MODELS}
_EFFORTS: list[tuple[str, str]] = [
    ("low", "Low"),
    ("medium", "Med"),
    ("high", "High"),
    ("xhigh", "X-High"),
    ("max", "Max"),
]
_EFFORT_KEYS = {key for key, _ in _EFFORTS}
_MODES: list[tuple[str, str]] = [("code", "Coding"), ("plan", "Plan")]

# Legacy sidecar model values → current picker keys. Covers the old short keys
# and the pre-key model strings; pre-Fable Opus sessions render as the nearest
# current option so the live panel never shows a raw/unknown value.
_MODEL_LEGACY = {
    "opus": "fable5",
    "opus48": "fable5",
    "claude-opus-4-8": "fable5",
    "opus48-1m": "fable5-1m",
    "claude-opus-4-8[1m]": "fable5-1m",
}
_EFFORT_LEGACY = {"extra-high": "xhigh"}


# ── callback codec (< 64 bytes; window_id is the trailing, colon-safe field) ─


def encode(action: str, payload: str | None, window_id: str) -> str:
    if payload is None:
        return f"{_CB_PREFIX}{action}:{window_id}"
    return f"{_CB_PREFIX}{action}:{payload}:{window_id}"


def decode(data: str) -> tuple[str, str | None, str] | None:
    """Return (action, payload, window_id) or None.

    The window_id is the trailing segment and may itself contain ``:`` (foreign
    ``session:@N`` ids), so it is never split — only the fixed leading fields
    (action, optional payload from a closed colon-free vocab) are peeled off.
    """
    if not data.startswith(_CB_PREFIX):
        return None
    rest = data[len(_CB_PREFIX) :]
    head, _, tail = rest.partition(":")
    if not tail:
        return None
    if head in ("open", "x"):
        return head, None, tail
    if head in ("m", "e", "mo"):
        payload, _, window_id = tail.partition(":")
        if not window_id:
            return None
        return head, payload, window_id
    return None


# ── button + keyboard ───────────────────────────────────────────────────────


def button_for_window(window_id: str) -> Any:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton

    # Icon-only — mobile clips button text. ⚙️ reads as "settings" on its own.
    return InlineKeyboardButton("⚙️", callback_data=encode("open", None, window_id))


def _norm_model(value: str) -> str:
    return _MODEL_LEGACY.get(value, value)


def _norm_effort(value: str) -> str:
    return _EFFORT_LEGACY.get(value, value)


def _radio_row(
    options: list[tuple[str, str]], selected: str, action: str, window_id: str
) -> list[Any]:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton

    return [
        InlineKeyboardButton(
            f"{'● ' if key == selected else ''}{label}",
            callback_data=encode(action, key, window_id),
        )
        for key, label in options
    ]


def build_settings_keyboard(window_id: str, sidecar: state.WindowSidecar | None) -> Any:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    model = _norm_model(sidecar.model) if sidecar else "fable5"
    effort = _norm_effort(sidecar.reasoning) if sidecar else "high"
    mode = "plan" if (sidecar and sidecar.mode == "plan") else "code"

    rows = [
        _radio_row([(k, label) for k, label, _m in _MODELS], model, "m", window_id),
        _radio_row(_EFFORTS, effort, "e", window_id),
        _radio_row(_MODES, mode, "mo", window_id),
        [InlineKeyboardButton("🌿 Git / PR", callback_data=_GIT_MENU_CB)],
        [InlineKeyboardButton("✖ Close", callback_data=encode("x", None, window_id))],
    ]
    return InlineKeyboardMarkup(rows)


def _menu_text(window_id: str, sidecar: state.WindowSidecar | None) -> str:
    # Lazy: thread_router is wired by ccgram bootstrap.
    from ccgram.thread_router import thread_router

    name = thread_router.get_display_name(window_id) or window_id
    model = _norm_model(sidecar.model) if sidecar else "fable5"
    effort = _norm_effort(sidecar.reasoning) if sidecar else "high"
    mode = "Plan" if (sidecar and sidecar.mode == "plan") else "Coding"
    model_label = next((label for k, label, _m in _MODELS if k == model), model)
    effort_label = next((label for k, label in _EFFORTS if k == effort), effort)
    return (
        f"⚙️ *Settings* — {name}\n\n"
        f"🧠 *Model:* {model_label}\n"
        f"⚡ *Reasoning:* {effort_label}\n"
        f"🧭 *Mode:* {mode}\n\n"
        "_Applies live to the running session._"
    )


# ── live apply ───────────────────────────────────────────────────────────────


async def apply_model(window_id: str, model_key: str) -> bool:
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.tmux_manager import send_to_window

    ok, _msg = await send_to_window(window_id, f"/model {_MODEL_ID[model_key]}")
    return ok


async def apply_effort(window_id: str, effort_key: str) -> bool:
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.tmux_manager import send_to_window

    ok, _msg = await send_to_window(window_id, f"/effort {effort_key}")
    return ok


async def apply_mode(window_id: str, mode_key: str) -> bool:
    # Lazy: layer module deferred to the call path.
    from .plan_mode import drive_to_mode

    target = "plan" if mode_key == "plan" else "coding"
    return await drive_to_mode(window_id, target)


# ── callbacks ─────────────────────────────────────────────────────────────────


async def handle_settings_callback(update: Any, _context: Any) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import ApplicationHandlerStop

    try:
        await _dispatch(update)
    finally:
        raise ApplicationHandlerStop


async def _dispatch(update: Any) -> None:
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.callback_helpers import user_owns_window

    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.window_query import view_window

    query = update.callback_query
    if query is None or not query.data:
        return
    decoded = decode(query.data)
    if decoded is None:
        await query.answer("Invalid", show_alert=True)
        return
    action, payload, window_id = decoded

    user = update.effective_user
    user_id = user.id if user else 0
    if not user_owns_window(user_id, window_id):
        await query.answer("Not your session", show_alert=True)
        return
    view = view_window(window_id)
    if view is None:
        await query.answer("⚠️ Session is no longer active", show_alert=True)
        return
    # Model / reasoning / mode are Claude-specific levers (/model, /effort,
    # plan mode). Don't drive them into a Codex/Gemini/shell pane.
    if view.provider_name != "claude":
        await query.answer("Settings apply to Claude sessions only", show_alert=True)
        return

    if action == "open":
        await _open_menu(query, window_id)
        return
    if action == "x":
        await _close_menu(query)
        return
    await _apply_change(query, window_id, action, payload)


async def _open_menu(query: Any, window_id: str) -> None:
    sidecar = state.load(window_id)
    await query.answer()
    await _send_menu(query, window_id, sidecar)


async def _send_menu(
    query: Any, window_id: str, sidecar: state.WindowSidecar | None
) -> None:
    """Post the menu as a separate reply to the host message."""
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.constants import ParseMode

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.error import TelegramError

    msg = query.message
    if msg is None:
        return
    thread_id = getattr(msg, "message_thread_id", None)
    try:
        await msg.reply_text(
            text=_menu_text(window_id, sidecar),
            reply_markup=build_settings_keyboard(window_id, sidecar),
            parse_mode=ParseMode.MARKDOWN,
            message_thread_id=thread_id,
        )
    except TelegramError as exc:
        logger.debug("settings menu send failed: %s", exc)


async def _close_menu(query: Any) -> None:
    # Lazy: only needed in this branch.
    import contextlib

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.error import TelegramError

    with contextlib.suppress(TelegramError):
        await query.delete_message()
    await query.answer("Closed")


async def _apply_change(
    query: Any, window_id: str, action: str, payload: str | None
) -> None:
    if payload is None:
        await query.answer("Invalid", show_alert=True)
        return
    # No idle gate: model/effort are client-side slash commands and mode is a
    # Shift+Tab keystroke — Claude Code accepts all three at any time, busy or
    # idle, exactly like driving them by hand in the app. (The old gate keyed
    # off a scraped permission-mode marker, which a default-mode "Coding"
    # session never shows, so it wrongly reported every such session as busy.)
    ok = False
    toast = ""
    if action == "m" and payload in _MODEL_ID:
        ok = await apply_model(window_id, payload)
        if ok:
            await state.update_locked(window_id, model=payload)
            toast = f"Model → {next(label for k, label, _m in _MODELS if k == payload)}"
    elif action == "e" and payload in _EFFORT_KEYS:
        ok = await apply_effort(window_id, payload)
        if ok:
            await state.update_locked(window_id, reasoning=payload)
            toast = (
                f"Reasoning → {next(label for k, label in _EFFORTS if k == payload)}"
            )
    elif action == "mo" and payload in ("plan", "code"):
        ok = await apply_mode(window_id, payload)
        if ok:
            await state.update_locked(
                window_id, mode="plan" if payload == "plan" else "coding"
            )
            toast = "Mode → " + ("Plan" if payload == "plan" else "Coding")
    else:
        await query.answer("Invalid", show_alert=True)
        return

    if not ok:
        await query.answer(
            "Couldn't apply — try again or use the pane.", show_alert=True
        )
        return
    await query.answer(f"✅ {toast}")
    await _rerender_menu(query, window_id)


async def _rerender_menu(query: Any, window_id: str) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.constants import ParseMode

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.error import TelegramError

    sidecar = state.load(window_id)
    try:
        await query.edit_message_text(
            text=_menu_text(window_id, sidecar),
            reply_markup=build_settings_keyboard(window_id, sidecar),
            parse_mode=ParseMode.MARKDOWN,
        )
    except TelegramError as exc:
        logger.debug("settings menu re-render no-op: %s", exc)


def install_settings_panel(application: Any) -> None:
    global _installed
    if _installed:
        return
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import CallbackQueryHandler

    application.add_handler(
        CallbackQueryHandler(handle_settings_callback, pattern=r"^ccgrampro:set:"),
        group=-10,
    )
    _installed = True
    logger.info("ccgram-pro settings panel installed — live model/reasoning/mode")


def _reset_for_testing() -> None:
    global _installed
    _installed = False

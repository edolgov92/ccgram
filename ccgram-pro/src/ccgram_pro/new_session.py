"""One-step new-session picker — project + model + reasoning in one message.

Replaces ccgram's folder-browser first step (which makes you navigate a
directory tree) with a single inline keyboard driven by the predefined
``projects.toml`` list:

    🆕 New session
    Project:   [● HP Backend] [HP App] [ccgram] …
    Model:     [● Opus 4.8] [Opus 4.8 · 1M]
    Reasoning: [Low] [Med] [High] [● X-High] [Max]
    [✅ Start session]   [Cancel]

Tapping a button toggles that row's selection (radio) and re-renders the
keyboard in place. "Start session" hands the chosen directory to
ccgram's existing confirm flow (so the worktree step still runs), and a
per-session ``--model`` / ``--effort`` override is injected into the
Claude launch command.

How the model/effort override reaches the launch:
- ``resolve_launch_command`` is wrapped to rewrite ``--model`` /
  ``--effort`` from two module globals.
- ``_create_window_and_bind`` is wrapped to set those globals from the
  selection stored on ``context.user_data`` immediately before calling
  the original (resolve runs synchronously inside it, so no other
  coroutine can observe the globals), then clear them.

Only active when ``projects.toml`` has entries; otherwise the original
folder browser is used unchanged.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING, Any

import structlog

from .config import load_projects

if TYPE_CHECKING:
    from telegram.ext import Application

logger = structlog.get_logger()

_SEL_KEY = "ccgrampro_new_session"
_CB_PREFIX = "ccgrampro:new:"

# (key, label, claude --model string)
_MODELS: list[tuple[str, str, str]] = [
    ("opus48", "Opus 4.8", "claude-opus-4-8"),
    ("opus48-1m", "Opus 4.8 · 1M", "claude-opus-4-8[1m]"),
]
_MODEL_STR = {key: model for key, _label, model in _MODELS}

# (effort key passed to --effort, short button label)
_EFFORTS: list[tuple[str, str]] = [
    ("low", "Low"),
    ("medium", "Med"),
    ("high", "High"),
    ("xhigh", "X-High"),
    ("max", "Max"),
]
_EFFORT_KEYS = {key for key, _ in _EFFORTS}

_DEFAULT_MODEL = "opus48"
_DEFAULT_EFFORT = "xhigh"

_installed = False

# Per-launch override globals. Set synchronously right before the
# original _create_window_and_bind runs (which calls resolve_launch_command
# without an intervening await) and cleared in its finally block.
_override_model: str | None = None
_override_effort: str | None = None


# ── selection state ──────────────────────────────────────────────────────


def _default_selection() -> dict[str, Any]:
    return {"project": 0, "model": _DEFAULT_MODEL, "effort": _DEFAULT_EFFORT}


def _get_selection(user_data: dict | None) -> dict[str, Any]:
    if user_data is None:
        return _default_selection()
    sel = user_data.get(_SEL_KEY)
    if not isinstance(sel, dict):
        sel = _default_selection()
        user_data[_SEL_KEY] = sel
    return sel


# ── keyboard rendering ─────────────────────────────────────────────────────


def _build_keyboard(sel: dict[str, Any]) -> Any:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    projects = load_projects()
    rows: list[list[InlineKeyboardButton]] = []

    # Projects — one per row (labels can be long), radio-marked.
    for idx, project in enumerate(projects):
        mark = "🟢 " if idx == sel["project"] else "📁 "
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark}{project.label}",
                    callback_data=f"{_CB_PREFIX}project:{idx}",
                )
            ]
        )

    # Model row.
    rows.append(
        [
            InlineKeyboardButton(
                f"{'● ' if sel['model'] == key else ''}{label}",
                callback_data=f"{_CB_PREFIX}model:{key}",
            )
            for key, label, _model in _MODELS
        ]
    )

    # Reasoning row.
    rows.append(
        [
            InlineKeyboardButton(
                f"{'● ' if sel['effort'] == key else ''}{label}",
                callback_data=f"{_CB_PREFIX}effort:{key}",
            )
            for key, label in _EFFORTS
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                "✅ Start session", callback_data=f"{_CB_PREFIX}start"
            ),
            InlineKeyboardButton("Cancel", callback_data=f"{_CB_PREFIX}cancel"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _render_text(sel: dict[str, Any]) -> str:
    projects = load_projects()
    project_label = (
        projects[sel["project"]].label if 0 <= sel["project"] < len(projects) else "—"
    )
    model_label = next(
        (label for key, label, _m in _MODELS if key == sel["model"]), sel["model"]
    )
    effort_label = next(
        (label for key, label in _EFFORTS if key == sel["effort"]), sel["effort"]
    )
    return (
        "*🆕 New session*\n\n"
        "Pick your project, model and reasoning, then tap *Start session*.\n\n"
        f"📁 *Project:* {project_label}\n"
        f"🧠 *Model:* {model_label}\n"
        f"⚡ *Reasoning:* {effort_label}"
    )


# ── show the picker (replaces the folder browser) ──────────────────────────


async def show_picker(
    *, thread_id: int, text: str, user_data: dict | None, message: Any
) -> bool:
    """Post the combined picker for an unbound topic. Returns True (handled).

    Stores the pending thread + first message (so it's delivered to the
    agent after the window is created) and a default selection on
    ``user_data``. ``STATE`` is set to the browsing-directory guard so a
    second text message doesn't spawn a duplicate picker.
    """
    # Lazy: ccgram internals — deferred to avoid an import cycle with bootstrap.
    from ccgram.handlers.messaging_pipeline.message_sender import safe_reply

    # Lazy: STATE_KEY / STATE_BROWSING_DIRECTORY live in directory_browser.
    from ccgram.handlers.topics.directory_browser import (
        STATE_BROWSING_DIRECTORY,
        STATE_KEY,
    )

    # Lazy: pending-thread keys live in user_state.
    from ccgram.handlers.user_state import PENDING_THREAD_ID, PENDING_THREAD_TEXT

    if user_data is not None:
        user_data[_SEL_KEY] = _default_selection()
        user_data[PENDING_THREAD_ID] = thread_id
        user_data[PENDING_THREAD_TEXT] = text
        user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
    sel = _get_selection(user_data)
    await safe_reply(message, _render_text(sel), reply_markup=_build_keyboard(sel))
    return True


# ── callback handling ──────────────────────────────────────────────────────


async def handle_new_session_callback(update: Any, context: Any) -> None:
    """Dispatch ``ccgrampro:new:*`` taps, then stop further handlers.

    Registered in an earlier handler group than ccgram's catch-all
    CallbackQueryHandler; ``ApplicationHandlerStop`` prevents that
    catch-all from also processing our callback.
    """
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import ApplicationHandlerStop

    await _dispatch_new_session(update, context)
    raise ApplicationHandlerStop


async def _dispatch_new_session(update: Any, context: Any) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    action = query.data[len(_CB_PREFIX) :]
    user_data = context.user_data

    if action == "cancel":
        await _handle_cancel(query, user_data)
        return
    if action == "start":
        await _handle_start(query, update, context)
        return

    sel = _get_selection(user_data)
    if action.startswith("project:"):
        try:
            sel["project"] = int(action.split(":", 1)[1])
        except ValueError:
            await query.answer("bad project")
            return
    elif action.startswith("model:"):
        key = action.split(":", 1)[1]
        if key in _MODEL_STR:
            sel["model"] = key
    elif action.startswith("effort:"):
        key = action.split(":", 1)[1]
        if key in _EFFORT_KEYS:
            sel["effort"] = key
    else:
        await query.answer("unknown action")
        return

    if user_data is not None:
        user_data[_SEL_KEY] = sel
    await query.answer()
    await _safe_rerender(query, sel)


async def _safe_rerender(query: Any, sel: dict[str, Any]) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.constants import ParseMode

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.error import TelegramError

    try:
        await query.edit_message_text(
            text=_render_text(sel),
            reply_markup=_build_keyboard(sel),
            parse_mode=ParseMode.MARKDOWN,
        )
    except TelegramError as exc:
        # "message is not modified" etc. — harmless.
        logger.debug("picker re-render no-op: %s", exc)


async def _handle_cancel(query: Any, user_data: dict | None) -> None:
    # Lazy: contextlib only needed in this branch.
    import contextlib

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.error import TelegramError

    if user_data is not None:
        user_data.pop(_SEL_KEY, None)
    with contextlib.suppress(TelegramError):
        await query.edit_message_text(text="❌ Cancelled.", reply_markup=None)
    await query.answer("Cancelled")


async def _handle_start(query: Any, update: Any, context: Any) -> None:
    """Resolve the project path and hand off to ccgram's confirm flow."""
    # Lazy: ccgram internals — deferred to avoid an import cycle with bootstrap.
    from ccgram.handlers.topics.directory_browser import BROWSE_PATH_KEY

    # Lazy: ccgram internal — deferred to avoid an import cycle with ccgram bootstrap.
    from ccgram.handlers.topics.directory_callbacks import _handle_confirm

    sel = _get_selection(context.user_data)
    projects = load_projects()
    if not (0 <= sel["project"] < len(projects)):
        await query.answer("Project no longer configured", show_alert=True)
        return
    project = projects[sel["project"]]

    if context.user_data is not None:
        # The confirm flow reads BROWSE_PATH_KEY as the chosen directory.
        context.user_data[BROWSE_PATH_KEY] = str(project.path)

    user = update.effective_user
    user_id = user.id if user else 0
    await query.answer("Starting…")
    # Continue exactly as if a directory had been confirmed: worktree
    # eligibility → (worktree picker) → forced provider/mode →
    # _create_window_and_bind (where the model/effort override applies).
    await _handle_confirm(query, user_id, update, context)


# ── launch-command model/effort override ───────────────────────────────────


def _apply_overrides(command: str, model: str, effort: str) -> str:
    """Rewrite ``--model`` / ``--effort`` in *command* with the chosen values."""
    quoted_model = shlex.quote(model)
    if re.search(r"--model\s+\S+", command):
        command = re.sub(r"--model\s+\S+", f"--model {quoted_model}", command, count=1)
    else:
        command = f"{command} --model {quoted_model}"
    if re.search(r"--effort\s+\S+", command):
        command = re.sub(r"--effort\s+\S+", f"--effort {effort}", command, count=1)
    else:
        command = f"{command} --effort {effort}"
    return command


# ── install ────────────────────────────────────────────────────────────────


def install_new_session(application: "Application") -> None:
    """Wire the picker, the unbound-topic replacement, and the launch override."""
    global _installed
    if _installed:
        return

    # Lazy: ccgram internals.
    from ccgram import providers as providers_mod

    # Lazy: ccgram internal — deferred to avoid an import cycle with ccgram bootstrap.
    from ccgram.handlers.text import text_handler as text_handler_mod

    # Lazy: ccgram internal — deferred to avoid an import cycle with ccgram bootstrap.
    from ccgram.handlers.topics import directory_callbacks as dc_mod

    # 1) Replace the unbound-topic UI with the picker when projects exist.
    original_unbound = text_handler_mod._handle_unbound_topic

    async def wrapped_unbound(
        user_id: int, thread_id: int, text: str, user_data: dict | None, message: Any
    ) -> bool:
        if load_projects():
            return await show_picker(
                thread_id=thread_id,
                text=text,
                user_data=user_data,
                message=message,
            )
        return await original_unbound(user_id, thread_id, text, user_data, message)

    text_handler_mod._handle_unbound_topic = wrapped_unbound  # type: ignore[assignment]

    # 2) Wrap resolve_launch_command to apply the per-launch override.
    original_resolve = providers_mod.resolve_launch_command

    def wrapped_resolve(provider_name: str, **kwargs: Any) -> str:
        command = original_resolve(provider_name, **kwargs)
        if provider_name == "claude" and _override_model:
            command = _apply_overrides(command, _override_model, _override_effort or "")
        return command

    providers_mod.resolve_launch_command = wrapped_resolve  # type: ignore[assignment]
    # directory_callbacks imports resolve_launch_command lazily inside
    # _create_window_and_bind (``from ccgram.providers import
    # resolve_launch_command``), so patching the providers module
    # attribute is enough — the lazy import resolves it at call time.

    # 3) Wrap _create_window_and_bind to set the override globals from the
    #    stored selection just before the original runs.
    original_cwb = dc_mod._create_window_and_bind

    async def wrapped_cwb(*args: Any, **kwargs: Any) -> Any:
        global _override_model, _override_effort
        provider_name = (
            str(args[3]) if len(args) >= 4 else kwargs.get("provider_name", "")
        )
        context = args[5] if len(args) >= 6 else kwargs.get("context")
        sel = None
        if context is not None and getattr(context, "user_data", None):
            sel = context.user_data.get(_SEL_KEY)
        if sel and provider_name == "claude":
            _override_model = _MODEL_STR.get(sel.get("model", ""), "")
            _override_effort = sel.get("effort", "")
        try:
            return await original_cwb(*args, **kwargs)
        finally:
            _override_model = None
            _override_effort = None
            if context is not None and getattr(context, "user_data", None):
                context.user_data.pop(_SEL_KEY, None)

    dc_mod._create_window_and_bind = wrapped_cwb  # type: ignore[assignment]

    # 4) Register the callback handler.
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import CallbackQueryHandler

    # group=-10: run before ccgram's catch-all CallbackQueryHandler (group 0).
    application.add_handler(
        CallbackQueryHandler(handle_new_session_callback, pattern=r"^ccgrampro:new:"),
        group=-10,
    )

    _installed = True
    logger.info(
        "ccgram-pro new-session picker installed — project + model + reasoning in one step"
    )


def _reset_for_testing() -> None:
    global _installed, _override_model, _override_effort
    _installed = False
    _override_model = None
    _override_effort = None

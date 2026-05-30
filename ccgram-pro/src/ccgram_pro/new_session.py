"""One-step new-session picker — project, model, reasoning, mode, workspace.

Replaces ccgram's folder-browser first step with a single inline keyboard
driven by ``projects.toml``:

    🆕 New session
    Project:   [🟢 HP Backend] [HP App] …
    Model:     [● Opus 4.8] [Opus 4.8 · 1M]
    Reasoning: [Low][Med][High][● X-High][Max]
    Mode:      [● Coding][Plan]
    Workspace: [● Current repo][Worktree][Clone]
    Base:      [main ▸]
    [✅ Start session]  [Cancel]

State lives in :mod:`ccgram_pro.new_session_store`, keyed per ``(chat_id,
thread_id)`` — NOT ccgram's shared per-user ``context.user_data`` (whose
cross-topic stomping caused the "modal appears again and again" bug). The
picker phase never sets ccgram's ``STATE_KEY``.

At Start, the chosen workspace strategy resolves the working directory
(current repo / a fresh worktree off the chosen base / a per-session clone),
then we hand that cwd to ccgram's ``_create_window_and_bind`` directly — it
creates the tmux window, binds the topic, and forwards the pending message.
Per-session ``--model`` / ``--effort`` / ``--permission-mode plan`` and the
TL;DR system-prompt are injected into the Claude launch command.

Only active when ``projects.toml`` has entries; otherwise the original
folder browser is used unchanged.
"""

from __future__ import annotations

import re
import shlex
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from . import new_session_store as store
from . import state
from .config import load_projects, workspaces_dir

if TYPE_CHECKING:
    from telegram.ext import Application

logger = structlog.get_logger()

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

_MODES: list[tuple[str, str]] = [("coding", "Coding"), ("plan", "Plan")]
_MODE_KEYS = {key for key, _ in _MODES}

_WORKSPACES: list[tuple[str, str]] = [
    ("current", "Current repo"),
    ("worktree", "Worktree"),
    ("clone", "Clone"),
]
_WORKSPACE_KEYS = {key for key, _ in _WORKSPACES}
# Strategies offered when the project is not a git work tree (worktree + base
# need git; clone auto-downgrades to a filesystem copy).
_NON_GIT_WORKSPACES = {"current", "clone"}

_DEFAULT_MODEL = "opus48"
_DEFAULT_EFFORT = "xhigh"
_BASE_PAGE_SIZE = 6
_CB_BASE_CURRENT = "cur"

_installed = False

# Per-launch override globals. Set synchronously right before the original
# _create_window_and_bind runs (which calls resolve_launch_command without an
# intervening await) and cleared in the caller's finally block.
_override_model: str | None = None
_override_effort: str | None = None
_override_plan: bool = False


# ── launch-command override ────────────────────────────────────────────────


def _apply_overrides(
    command: str,
    model: str,
    effort: str,
    *,
    plan: bool = False,
    append_system_prompt: str | None = None,
) -> str:
    """Rewrite ``--model`` / ``--effort`` and add plan/system-prompt flags.

    ``--model`` / ``--effort`` are rewritten in place (or appended). When
    *plan* is set, ``--permission-mode plan`` is rewritten/appended so the
    session starts in plan mode deterministically. When *append_system_prompt*
    is given, a fresh ``--append-system-prompt`` token pair is appended
    (shlex-safe, idempotent) — Claude concatenates multiple appends, so an
    existing env-provided prompt is preserved untouched.
    """
    quoted_model = shlex.quote(model)
    if re.search(r"--model\s+\S+", command):
        command = re.sub(r"--model\s+\S+", f"--model {quoted_model}", command, count=1)
    else:
        command = f"{command} --model {quoted_model}"
    if re.search(r"--effort\s+\S+", command):
        command = re.sub(r"--effort\s+\S+", f"--effort {effort}", command, count=1)
    else:
        command = f"{command} --effort {effort}"
    if plan:
        if re.search(r"--permission-mode\s+\S+", command):
            command = re.sub(
                r"--permission-mode\s+\S+", "--permission-mode plan", command, count=1
            )
        else:
            command = f"{command} --permission-mode plan"
    if append_system_prompt and append_system_prompt not in command:
        command = (
            f"{command} --append-system-prompt {shlex.quote(append_system_prompt)}"
        )
    return command


def _wrapped_resolve(original: Any) -> Any:
    """Wrap ``resolve_launch_command`` to apply overrides + the TL;DR prompt."""

    def wrapped(provider_name: str, **kwargs: Any) -> str:
        command = original(provider_name, **kwargs)
        if provider_name != "claude":
            return command
        # The TL;DR contract is appended for every Claude launch so Claude
        # itself produces the user-facing summary (replacing the LLM hop).
        # Lazy: layer module deferred to the call path.
        from .output_pipeline.tldr import TLDR_SYSTEM_PROMPT

        if _override_model:
            command = _apply_overrides(
                command,
                _override_model,
                _override_effort or "",
                plan=_override_plan,
                append_system_prompt=TLDR_SYSTEM_PROMPT,
            )
        elif TLDR_SYSTEM_PROMPT not in command:
            command = (
                f"{command} --append-system-prompt {shlex.quote(TLDR_SYSTEM_PROMPT)}"
            )
        return command

    return wrapped


# ── keyboard rendering ──────────────────────────────────────────────────────


def _model_label(key: str) -> str:
    return next((label for k, label, _m in _MODELS if k == key), key)


def _effort_label(key: str) -> str:
    return next((label for k, label in _EFFORTS if k == key), key)


def _radio_row(options: list[tuple[str, str]], selected: str, action: str) -> list[Any]:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton

    return [
        InlineKeyboardButton(
            f"{'● ' if key == selected else ''}{label}",
            callback_data=f"{_CB_PREFIX}{action}:{key}",
        )
        for key, label in options
    ]


def _build_keyboard(session: store.PendingSession) -> Any:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if session.viewing_base:
        return _build_base_keyboard(session)

    projects = load_projects()
    rows: list[list[Any]] = []

    for idx, project in enumerate(projects):
        mark = "🟢 " if idx == session.project_idx else "📁 "
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark}{project.label}"[:60],
                    callback_data=f"{_CB_PREFIX}project:{idx}",
                )
            ]
        )

    rows.append(
        _radio_row([(k, label) for k, label, _m in _MODELS], session.model_key, "model")
    )
    rows.append(_radio_row(_EFFORTS, session.effort_key, "effort"))
    rows.append(_radio_row(_MODES, session.mode, "mode"))

    ws_options = (
        _WORKSPACES
        if session.project_is_git
        else [o for o in _WORKSPACES if o[0] in _NON_GIT_WORKSPACES]
    )
    rows.append(_radio_row(ws_options, session.workspace_strategy, "ws"))

    if session.project_is_git:
        base_label = session.base_branch or "(current)"
        rows.append(
            [
                InlineKeyboardButton(
                    f"⎇ Base: {base_label} ▸",
                    callback_data=f"{_CB_PREFIX}baseopen",
                )
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


def _build_base_keyboard(session: store.PendingSession) -> Any:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows: list[list[Any]] = [
        [
            InlineKeyboardButton(
                f"{'● ' if session.base_branch is None else ''}(current branch)",
                callback_data=f"{_CB_PREFIX}base:{_CB_BASE_CURRENT}",
            )
        ]
    ]
    choices = session.branch_choices
    start = session.base_page * _BASE_PAGE_SIZE
    page = choices[start : start + _BASE_PAGE_SIZE]
    for offset, name in enumerate(page):
        idx = start + offset
        mark = "● " if name == session.base_branch else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark}{name}"[:60],
                    callback_data=f"{_CB_PREFIX}base:{idx}",
                )
            ]
        )

    nav: list[Any] = []
    if session.base_page > 0:
        nav.append(
            InlineKeyboardButton(
                "‹ Prev", callback_data=f"{_CB_PREFIX}basepage:{session.base_page - 1}"
            )
        )
    if start + _BASE_PAGE_SIZE < len(choices):
        nav.append(
            InlineKeyboardButton(
                "Next ›", callback_data=f"{_CB_PREFIX}basepage:{session.base_page + 1}"
            )
        )
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("‹ Back", callback_data=f"{_CB_PREFIX}baseback")])
    return InlineKeyboardMarkup(rows)


def _render_text(session: store.PendingSession) -> str:
    projects = load_projects()
    project_label = (
        projects[session.project_idx].label
        if 0 <= session.project_idx < len(projects)
        else "—"
    )
    mode_label = "Plan" if session.mode == "plan" else "Coding"
    ws_label = next(
        (label for k, label in _WORKSPACES if k == session.workspace_strategy),
        session.workspace_strategy,
    )
    if session.viewing_base:
        return (
            "*⎇ Base branch*\n\n"
            f"Pick the branch to start *{project_label}* from, "
            "or keep the current branch."
        )
    base_label = session.base_branch or "(current)"
    lines = [
        "*🆕 New session*\n",
        "Pick your options, then tap *Start session*.\n",
        f"📁 *Project:* {project_label}",
        f"🧠 *Model:* {_model_label(session.model_key)}",
        f"⚡ *Reasoning:* {_effort_label(session.effort_key)}",
        f"🧭 *Mode:* {mode_label}",
        f"🗂 *Workspace:* {ws_label}",
    ]
    if session.project_is_git:
        lines.append(f"⎇ *Base:* {base_label}")
    return "\n".join(lines)


# ── show the picker ─────────────────────────────────────────────────────────


async def show_picker(
    *, chat_id: int, thread_id: int, user_id: int, text: str, message: Any
) -> bool:
    """Post the picker for an unbound topic. Returns True (handled).

    Idempotent: if a pending session already exists for this topic, the new
    message is queued (it'll be delivered after the session starts) and NO
    second picker is posted.
    """
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.messaging_pipeline.message_sender import safe_reply

    existing = store.get(chat_id, thread_id)
    if existing is not None:
        store.append_text(chat_id, thread_id, text)
        await safe_reply(
            message,
            "🆕 Use the new-session card above to start — your message will be "
            "delivered once the session is running.",
        )
        return True

    default_mode = "plan" if _default_plan_mode() else "coding"
    session = store.create(chat_id, thread_id, user_id, text, default_mode=default_mode)
    await _resolve_project_git(session)
    sent = await safe_reply(
        message, _render_text(session), reply_markup=_build_keyboard(session)
    )
    if sent is not None:
        session.picker_message_id = getattr(sent, "message_id", None)
    return True


def _default_plan_mode() -> bool:
    # Lazy: settings load reads TOML; only needed when a picker opens.
    from .config import load_settings

    return load_settings().defaults.plan_mode_on_new_session


async def _resolve_project_git(session: store.PendingSession) -> None:
    """Cache whether the selected project is a git work tree."""
    projects = load_projects()
    if not (0 <= session.project_idx < len(projects)):
        session.project_is_git = False
        return
    # Lazy: only needed in this branch.
    import asyncio

    # Lazy: layer module deferred to the call path.
    from .git_ops import is_git_repo

    path = projects[session.project_idx].path
    session.project_is_git = await asyncio.to_thread(is_git_repo, path)
    if not session.project_is_git and session.workspace_strategy == "worktree":
        session.workspace_strategy = "current"


# ── callback handling ───────────────────────────────────────────────────────


async def handle_new_session_callback(update: Any, context: Any) -> None:
    """Dispatch ``ccgrampro:new:*`` taps, then stop further handlers."""
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import ApplicationHandlerStop

    try:
        await _dispatch_new_session(update, context)
    finally:
        raise ApplicationHandlerStop


def _query_topic(query: Any) -> tuple[int, int, int]:
    """Return (chat_id, thread_id, message_id) for a picker callback."""
    msg = query.message
    chat_id = msg.chat.id if msg and msg.chat else 0
    thread_id = getattr(msg, "message_thread_id", None) or 0
    message_id = getattr(msg, "message_id", None) or 0
    return chat_id, thread_id, message_id


async def _dispatch_new_session(update: Any, context: Any) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    chat_id, thread_id, message_id = _query_topic(query)
    session = store.get(chat_id, thread_id)
    if session is None:
        await _expire(query)
        return
    if (
        session.picker_message_id is not None
        and session.picker_message_id != message_id
    ):
        await query.answer("This is an old card — use the latest one.")
        return

    action = query.data[len(_CB_PREFIX) :]

    if action == "cancel":
        store.clear(chat_id, thread_id)
        await _safe_edit_terminal(query, "❌ Cancelled.")
        await query.answer("Cancelled")
        return
    if action == "start":
        await _handle_start(query, update, context, session)
        return
    if action == "baseopen":
        await _open_base_view(query, session)
        return
    if action == "baseback":
        session.viewing_base = False
        await query.answer()
        await _safe_rerender(query, session)
        return

    await _apply_selection(query, session, action)


async def _apply_selection(
    query: Any, session: store.PendingSession, action: str
) -> None:
    if action.startswith("project:"):
        try:
            idx = int(action.split(":", 1)[1])
        except ValueError:
            await query.answer("bad project")
            return
        projects = load_projects()
        if 0 <= idx < len(projects):
            session.project_idx = idx
            session.base_branch = None
            session.branch_choices = []
            session.base_page = 0
            await _resolve_project_git(session)
    elif action.startswith("model:"):
        key = action.split(":", 1)[1]
        if key in _MODEL_STR:
            session.model_key = key
    elif action.startswith("effort:"):
        key = action.split(":", 1)[1]
        if key in _EFFORT_KEYS:
            session.effort_key = key
    elif action.startswith("mode:"):
        key = action.split(":", 1)[1]
        if key in _MODE_KEYS:
            session.mode = key
    elif action.startswith("ws:"):
        key = action.split(":", 1)[1]
        allowed = _WORKSPACE_KEYS if session.project_is_git else _NON_GIT_WORKSPACES
        if key in allowed:
            session.workspace_strategy = key
    elif action.startswith("basepage:"):
        try:
            session.base_page = max(0, int(action.split(":", 1)[1]))
        except ValueError:
            session.base_page = 0
    elif action.startswith("base:"):
        await _select_base(query, session, action.split(":", 1)[1])
        return
    else:
        await query.answer("unknown action")
        return

    await query.answer()
    await _safe_rerender(query, session)


async def _open_base_view(query: Any, session: store.PendingSession) -> None:
    if not session.project_is_git:
        await query.answer("Not a git project")
        return
    if not session.branch_choices:
        # Lazy: only needed in this branch.
        import asyncio

        # Lazy: layer module deferred to the call path.
        from .git_ops import GitOpError, list_branches

        projects = load_projects()
        path = projects[session.project_idx].path
        try:
            branches = await asyncio.to_thread(list_branches, path)
            session.branch_choices = [b.name for b in branches]
        except (GitOpError, OSError) as exc:
            logger.debug("could not list branches for %s: %s", path, exc)
            await query.answer("Could not list branches", show_alert=True)
            return
    session.viewing_base = True
    session.base_page = 0
    await query.answer()
    await _safe_rerender(query, session)


async def _select_base(query: Any, session: store.PendingSession, token: str) -> None:
    if token == _CB_BASE_CURRENT:
        session.base_branch = None
    else:
        try:
            idx = int(token)
        except ValueError:
            await query.answer("bad branch")
            return
        if 0 <= idx < len(session.branch_choices):
            session.base_branch = session.branch_choices[idx]
    session.viewing_base = False
    await query.answer()
    await _safe_rerender(query, session)


async def _safe_rerender(query: Any, session: store.PendingSession) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.constants import ParseMode

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.error import TelegramError

    try:
        await query.edit_message_text(
            text=_render_text(session),
            reply_markup=_build_keyboard(session),
            parse_mode=ParseMode.MARKDOWN,
        )
    except TelegramError as exc:
        logger.debug("picker re-render no-op: %s", exc)


async def _safe_edit_terminal(query: Any, text: str) -> None:
    # Lazy: only needed in this branch.
    import contextlib

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.error import TelegramError

    with contextlib.suppress(TelegramError):
        await query.edit_message_text(text=text, reply_markup=None)


async def _expire(query: Any) -> None:
    await query.answer(
        "This session card expired — send a new message.", show_alert=True
    )
    await _safe_edit_terminal(
        query, "⌛ This new-session card expired. Send a message to start again."
    )


# ── Start ───────────────────────────────────────────────────────────────────


async def _handle_start(
    query: Any, update: Any, context: Any, session: store.PendingSession
) -> None:
    """Resolve the workspace, create + bind the window, deliver the message."""
    if session.in_progress:
        await query.answer("Starting…")
        return
    session.in_progress = True

    projects = load_projects()
    if not (0 <= session.project_idx < len(projects)):
        store.clear(session.chat_id, session.thread_id)
        await _safe_edit_terminal(
            query,
            "⚠️ That project is no longer configured. Send a message to start again.",
        )
        await query.answer()
        return
    project = projects[session.project_idx]
    repo = project.path

    await query.answer("Starting…")

    clone_dest: Path | None = None
    worktree_dest: Path | None = None
    try:
        cwd = await _provision_cwd(session, project, repo)
    except _StartError as exc:
        session.in_progress = False
        await _safe_rerender_with_error(query, session, str(exc))
        return
    if session.workspace_strategy == "clone":
        clone_dest = cwd
    elif session.workspace_strategy == "worktree":
        worktree_dest = cwd

    # Hand off to ccgram's window creator. It reads PENDING_THREAD_* from
    # user_data, forwards the pending text, and binds the topic.
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.user_state import PENDING_THREAD_ID, PENDING_THREAD_TEXT

    user = update.effective_user
    user_id = user.id if user else session.user_id
    if context.user_data is not None:
        context.user_data[PENDING_THREAD_ID] = session.thread_id
        context.user_data[PENDING_THREAD_TEXT] = session.combined_text()

    provider, approval_mode = _resolve_provider_and_mode(session)

    # Edit the card BEFORE arming the override globals: the globals are
    # module-level and only safe because _create_window_and_bind resolves the
    # launch command synchronously (no await) right after we set them. Any
    # await between set-and-call would let a concurrent start in another topic
    # overwrite them. So this is the last await before the globals are armed.
    await _safe_edit_terminal(query, "⏳ Starting session…")

    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.topics.directory_callbacks import _create_window_and_bind

    global _override_model, _override_effort, _override_plan
    if provider == "claude":
        _override_model = _MODEL_STR.get(session.model_key, "")
        _override_effort = session.effort_key
        _override_plan = session.mode == "plan"
    try:
        await _create_window_and_bind(
            query, user_id, str(cwd), provider, approval_mode, context
        )
    finally:
        _override_model = None
        _override_effort = None
        _override_plan = False

    await _finalize_start(user_id, session, clone_dest, worktree_dest, repo)


def _resolve_provider_and_mode(session: store.PendingSession) -> tuple[str, str]:
    # Lazy: config is the source of forced provider/mode.
    from ccgram.config import config

    provider = config.forced_provider or "claude"
    if session.mode == "plan":
        approval_mode = "normal"
    else:
        approval_mode = config.forced_approval_mode or "yolo"
    return provider, approval_mode


async def _finalize_start(
    user_id: int,
    session: store.PendingSession,
    clone_dest: Path | None,
    worktree_dest: Path | None,
    repo: Path,
) -> None:
    """Resolve the created window, persist the sidecar, and clear the store.

    If no window got bound (creation failed — ccgram already edited the card
    with an error), clean up whatever we provisioned (clone dir or worktree)
    and drop the store entry so the next message re-shows the picker.
    """
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.thread_router import thread_router

    created_wid = thread_router.get_window_for_thread(user_id, session.thread_id)
    if created_wid is None:
        if clone_dest is not None:
            _cleanup_clone(clone_dest)
        if worktree_dest is not None:
            await _cleanup_worktree(repo, worktree_dest)
        store.clear(session.chat_id, session.thread_id)
        return

    async with state.transaction(created_wid):
        sidecar = state.get_or_create(created_wid)
        sidecar.project_path = str(repo)
        sidecar.model = session.model_key
        sidecar.reasoning = session.effort_key
        sidecar.mode = session.mode
        sidecar.workspace_strategy = session.workspace_strategy
        sidecar.base_branch = session.base_branch
        sidecar.plan_mode = "entered" if session.mode == "plan" else "skipped"
        if clone_dest is not None:
            sidecar.workspace_path = str(clone_dest)
            # Stamp activity now so the idle GC sweep doesn't reap a workspace
            # that was just provisioned (it keys off last_activity_at).
            sidecar.last_activity_at = time.time()
        state.save(sidecar)
    store.clear(session.chat_id, session.thread_id)
    logger.info(
        "new-session started: window=%s project=%s mode=%s workspace=%s base=%s",
        created_wid,
        repo,
        session.mode,
        session.workspace_strategy,
        session.base_branch or "(current)",
    )


class _StartError(RuntimeError):
    """A one-line, user-facing reason the session could not start."""


async def _provision_cwd(
    session: store.PendingSession, project: Any, repo: Path
) -> Path:
    """Resolve the session's working directory per the chosen strategy."""
    # Lazy: only needed in this branch.
    import asyncio

    strategy = session.workspace_strategy
    if strategy == "current":
        if session.base_branch:
            # Lazy: layer module deferred to the call path.
            from .git_ops import (
                GitOpError,
                checkout,
                current_branch,
                has_uncommitted_changes,
            )

            try:
                cur = await asyncio.to_thread(current_branch, repo)
                if session.base_branch != cur:
                    if await asyncio.to_thread(has_uncommitted_changes, repo):
                        raise _StartError(
                            "Working tree has uncommitted changes — commit/stash, "
                            "or pick the current branch."
                        )
                    await asyncio.to_thread(checkout, repo, session.base_branch)
            except GitOpError as exc:
                raise _StartError(
                    f"Could not switch to {session.base_branch!r}: {str(exc).splitlines()[0]}"
                ) from exc
        return repo

    if strategy == "worktree":
        # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
        from ccgram.handlers.topics.worktree import (
            WorktreeError,
            create_worktree,
            slug_for_path,
            suggest_branch_name,
            worktree_path_for,
        )

        branch = await asyncio.to_thread(suggest_branch_name, session.first_text, repo)
        wt_path = worktree_path_for(repo, slug_for_path(branch))
        base_ref = session.base_branch or "HEAD"
        try:
            await asyncio.to_thread(
                create_worktree, repo, branch, wt_path, base_ref=base_ref
            )
        except WorktreeError as exc:
            raise _StartError(
                f"Could not create worktree: {str(exc).splitlines()[0]}"
            ) from exc
        return wt_path

    # clone
    # Lazy: layer module deferred to the call path.
    from .git_ops import GitOpError, checkout

    # Lazy: layer module deferred to the call path.
    from .workspaces.manager import WorkspaceCreationError, provision_workspace

    dest = workspaces_dir() / f"pending-{uuid.uuid4().hex}"
    try:
        await provision_workspace(repo, dest, install_command=project.install_command)
    except WorkspaceCreationError as exc:
        raise _StartError(
            f"Could not provision workspace: {str(exc).splitlines()[0]}"
        ) from exc
    if session.base_branch:
        try:
            await asyncio.to_thread(checkout, dest, session.base_branch)
        except GitOpError as exc:
            _cleanup_clone(dest)
            raise _StartError(
                f"Clone could not switch to {session.base_branch!r}: {str(exc).splitlines()[0]}"
            ) from exc
    return dest


def _cleanup_clone(dest: Path) -> None:
    # Lazy: only needed in this branch.
    import shutil

    shutil.rmtree(dest, ignore_errors=True)


async def _cleanup_worktree(repo: Path, worktree_dest: Path) -> None:
    """Best-effort removal of a worktree created for a session that never bound.

    The worktree is freshly created (no work in it yet) when window creation
    fails, so ``git worktree remove --force`` is safe. Failures are swallowed —
    an orphan worktree is recoverable via ``git worktree list``.
    """
    # Lazy: only needed on this failure path.
    import asyncio

    # Lazy: layer module deferred to the call path.
    from .git_ops import GitOpError

    # Lazy: layer module deferred to the call path.
    from .git_ops._run import run_git

    try:
        await asyncio.to_thread(
            run_git, repo, "worktree", "remove", "--force", str(worktree_dest)
        )
    except GitOpError as exc:
        logger.debug("could not remove orphan worktree %s: %s", worktree_dest, exc)


async def _safe_rerender_with_error(
    query: Any, session: store.PendingSession, error: str
) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.constants import ParseMode

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.error import TelegramError

    text = f"❌ {error}\n\n{_render_text(session)}"
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=_build_keyboard(session),
            parse_mode=ParseMode.MARKDOWN,
        )
    except TelegramError as exc:
        logger.debug("error re-render no-op: %s", exc)


# ── install ──────────────────────────────────────────────────────────────────


def install_new_session(application: "Application") -> None:
    """Wire the picker, the unbound-topic replacement, and the launch override."""
    global _installed
    if _installed:
        return

    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram import providers as providers_mod

    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.text import text_handler as text_handler_mod

    # 1) Replace the unbound-topic UI with the picker when projects exist.
    original_unbound = text_handler_mod._handle_unbound_topic

    async def wrapped_unbound(
        user_id: int, thread_id: int, text: str, user_data: dict | None, message: Any
    ) -> bool:
        # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
        from ccgram.thread_router import thread_router

        # Already bound to a window → NOT an unbound topic. Return False so the
        # text orchestrator proceeds to the dead-window check and forwards the
        # message. (The original _handle_unbound_topic does this same check
        # first; dropping it made every message in a configured topic re-show
        # the picker and spawn a new window.)
        if thread_router.get_window_for_thread(user_id, thread_id) is not None:
            return False
        if load_projects():
            chat = getattr(message, "chat", None)
            chat_id = chat.id if chat is not None else 0
            return await show_picker(
                chat_id=chat_id,
                thread_id=thread_id,
                user_id=user_id,
                text=text,
                message=message,
            )
        return await original_unbound(user_id, thread_id, text, user_data, message)

    text_handler_mod._handle_unbound_topic = wrapped_unbound  # type: ignore[assignment]

    # 2) Wrap resolve_launch_command to apply overrides + the TL;DR prompt.
    providers_mod.resolve_launch_command = _wrapped_resolve(  # type: ignore[assignment]
        providers_mod.resolve_launch_command
    )

    # 3) Register the callback handler (group=-10: before ccgram's catch-all).
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import CallbackQueryHandler

    application.add_handler(
        CallbackQueryHandler(handle_new_session_callback, pattern=r"^ccgrampro:new:"),
        group=-10,
    )

    _installed = True
    logger.info(
        "ccgram-pro new-session picker installed — project/model/reasoning/mode/workspace"
    )


def _reset_for_testing() -> None:
    global _installed, _override_model, _override_effort, _override_plan
    _installed = False
    _override_model = None
    _override_effort = None
    _override_plan = False
    store._reset_for_testing()

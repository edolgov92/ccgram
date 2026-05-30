"""Telegram Git/PR composer — branch, commit, push, open PR via buttons.

Reached from the ⚙️ Settings menu's "Git / PR" button. Resolves the repo from
the window's workspace/cwd and drives :mod:`ccgram_pro.git_ops` with friendly,
button-first steps and one-line error surfacing. Free-text steps (branch name,
commit message, PR title/body) are captured per-thread via
:mod:`ccgram_pro.git_composer_state` and consumed by a high-priority message
handler so they never reach the agent.

All git/gh calls are synchronous and run via ``asyncio.to_thread`` so the
event loop is never blocked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import structlog

from . import git_composer_state as gstate
from . import state

logger = structlog.get_logger()

_CB_PREFIX = "ccgrampro:git:"
_installed = False


def _one_line(exc: Exception) -> str:
    text = str(exc).strip()
    first = text.splitlines()[0] if text else type(exc).__name__
    return first[:300]


def _resolve_repo(window_id: str) -> str | None:
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.window_query import view_window

    sidecar = state.load(window_id)
    if sidecar and sidecar.workspace_path:
        return sidecar.workspace_path
    view = view_window(window_id)
    return view.cwd if view and view.cwd else None


def _topic(update: Any) -> tuple[int, int]:
    """Return (user_id, thread_id) for a callback or message update."""
    user = update.effective_user
    user_id = user.id if user else 0
    msg = update.callback_query.message if update.callback_query else update.message
    thread_id = getattr(msg, "message_thread_id", None) or 0
    return user_id, thread_id


def _resolve_window(update: Any) -> tuple[int, int, str | None]:
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.thread_router import thread_router

    user_id, thread_id = _topic(update)
    window_id = thread_router.get_window_for_thread(user_id, thread_id)
    return user_id, thread_id, window_id


def _default_base(repo: str, branches: list[str]) -> str:
    # Lazy: layer module deferred to the call path.
    from .git_ops import GitOpError, current_branch

    # Lazy: layer module deferred to the call path.
    from .git_ops._run import run_git

    try:
        result = run_git(repo, "rev-parse", "--abbrev-ref", "origin/HEAD", check=False)
        ref = result.stdout.strip()
        if result.returncode == 0 and ref.startswith("origin/"):
            return ref[len("origin/") :]
    except GitOpError:
        pass
    for candidate in ("main", "master", "develop"):
        if candidate in branches:
            return candidate
    try:
        return current_branch(repo)
    except GitOpError:
        return branches[0] if branches else "main"


# ── menu ──────────────────────────────────────────────────────────────────


async def _render_menu(query: Any, repo: str) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.constants import ParseMode

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.error import TelegramError

    # Lazy: layer module deferred to the call path.
    from .git_ops import (
        current_branch,
        is_detached_head,
        is_git_repo,
        working_tree_status,
    )

    if not await asyncio.to_thread(is_git_repo, repo):
        await _safe_edit(query, "📂 This session's directory is not a git repository.")
        return

    branch = await asyncio.to_thread(current_branch, repo)
    detached = await asyncio.to_thread(is_detached_head, repo)
    status = await asyncio.to_thread(working_tree_status, repo)

    header = f"🌿 *Branch:* `{branch}`"
    if detached:
        header += "  ⚠️ detached HEAD"
    changes = (
        "✅ clean"
        if status.clean
        else f"{status.staged}S · {status.unstaged}U · {status.untracked}?"
    )
    text = f"🌿 *Git / PR*\n\n{header}\n📝 *Changes:* {changes}"

    rows: list[list[Any]] = [
        [InlineKeyboardButton("🌱 Create branch", callback_data=f"{_CB_PREFIX}branch")]
    ]
    if not status.clean:
        rows.append(
            [
                InlineKeyboardButton(
                    "💾 Commit & push", callback_data=f"{_CB_PREFIX}commit"
                )
            ]
        )
    rows.append([InlineKeyboardButton("⬆️ Push", callback_data=f"{_CB_PREFIX}push")])
    rows.append([InlineKeyboardButton("🔀 Open PR", callback_data=f"{_CB_PREFIX}pr")])
    rows.append(
        [InlineKeyboardButton("🌐 Web composer", callback_data=f"{_CB_PREFIX}web")]
    )
    rows.append([InlineKeyboardButton("✖ Close", callback_data=f"{_CB_PREFIX}cancel")])

    try:
        await query.edit_message_text(
            text=text,
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode=ParseMode.MARKDOWN,
        )
    except TelegramError:
        # The host message (e.g. settings menu) may not be editable into this;
        # fall back to a fresh reply.
        msg = query.message
        if msg is not None:
            await msg.reply_text(
                text=text,
                reply_markup=InlineKeyboardMarkup(rows),
                parse_mode=ParseMode.MARKDOWN,
                message_thread_id=getattr(msg, "message_thread_id", None),
            )


async def _safe_edit(query: Any, text: str, reply_markup: Any = None) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.constants import ParseMode

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.error import TelegramError

    try:
        await query.edit_message_text(
            text=text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
        )
    except TelegramError as exc:
        logger.debug("git composer edit no-op: %s", exc)


# ── callback dispatch ────────────────────────────────────────────────────────


async def handle_git_callback(update: Any, _context: Any) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import ApplicationHandlerStop

    try:
        await _dispatch(update)
    finally:
        raise ApplicationHandlerStop


async def _dispatch(update: Any) -> None:
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.callback_helpers import user_owns_window

    query = update.callback_query
    if query is None or not query.data:
        return
    user_id, thread_id, window_id = _resolve_window(update)
    if window_id is None or not user_owns_window(user_id, window_id):
        await query.answer("⚠️ No active session here", show_alert=True)
        return
    repo = _resolve_repo(window_id)
    if repo is None:
        await query.answer("⚠️ No repository for this session", show_alert=True)
        return

    action = query.data[len(_CB_PREFIX) :]
    try:
        await _route(action, query, user_id, thread_id, window_id, repo)
    except (OSError, ValueError) as exc:
        await query.answer(_one_line(exc), show_alert=True)


async def _route(
    action: str,
    query: Any,
    user_id: int,
    thread_id: int,
    window_id: str,
    repo: str,
) -> None:
    if action == "menu":
        await query.answer()
        await _render_menu(query, repo)
    elif action == "branch":
        await _start_branch(query, user_id, thread_id, window_id, repo)
    elif action == "branch_ok":
        await _create_suggested_branch(query, user_id, thread_id, repo)
    elif action == "branch_edit":
        await _arm_branch_edit(query, user_id, thread_id, window_id, repo)
    elif action == "commit":
        await _arm_commit(query, user_id, thread_id, window_id, repo)
    elif action == "commit_auto":
        await _commit_and_push(query, repo, message=None)
    elif action == "push":
        await _push(query, repo)
    elif action == "pr":
        await _start_pr(query, user_id, thread_id, window_id, repo)
    elif action == "pr_base":
        await _cycle_base(query, user_id, thread_id)
    elif action == "pr_draft":
        await _toggle_draft(query, user_id, thread_id)
    elif action == "pr_ok":
        await _create_pr(query, user_id, thread_id, repo)
    elif action == "web":
        await _open_web_composer(query, window_id)
    elif action == "cancel":
        gstate.disarm(user_id, thread_id)
        await query.answer("Closed")
        await _close(query)
    else:
        await query.answer("unknown action")


# ── branch ───────────────────────────────────────────────────────────────────


async def _start_branch(
    query: Any, user_id: int, thread_id: int, window_id: str, repo: str
) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.topics.worktree import suggest_branch_name

    suggested = await asyncio.to_thread(suggest_branch_name, None, Path(repo))
    gstate.arm(
        user_id,
        thread_id,
        gstate.ComposerInput(
            awaiting="branch_name",
            window_id=window_id,
            repo=repo,
            suggested_branch=suggested,
        ),
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"✅ Use {suggested}", callback_data=f"{_CB_PREFIX}branch_ok"
                )
            ],
            [
                InlineKeyboardButton(
                    "✏️ Edit name", callback_data=f"{_CB_PREFIX}branch_edit"
                )
            ],
            [InlineKeyboardButton("✖ Cancel", callback_data=f"{_CB_PREFIX}cancel")],
        ]
    )
    await query.answer()
    await _safe_edit(query, f"🌱 Create branch `{suggested}`?", reply_markup=kb)


async def _arm_branch_edit(
    query: Any, user_id: int, thread_id: int, window_id: str, repo: str
) -> None:
    pending = gstate.peek(user_id, thread_id)
    suggested = pending.suggested_branch if pending else ""
    gstate.arm(
        user_id,
        thread_id,
        gstate.ComposerInput(
            awaiting="branch_name",
            window_id=window_id,
            repo=repo,
            suggested_branch=suggested,
        ),
    )
    await query.answer()
    await _safe_edit(query, "✏️ Send the branch name as a message.")


async def _create_suggested_branch(
    query: Any, user_id: int, thread_id: int, repo: str
) -> None:
    pending = gstate.peek(user_id, thread_id)
    if pending is None or not pending.suggested_branch:
        await query.answer("Expired — reopen Git menu", show_alert=True)
        return
    await _do_create_branch(query, user_id, thread_id, repo, pending.suggested_branch)


async def _do_create_branch(
    query: Any, user_id: int, thread_id: int, repo: str, name: str
) -> None:
    # Lazy: layer module deferred to the call path.
    from .git_ops import GitOpError, create_branch

    try:
        await asyncio.to_thread(create_branch, repo, name, checkout=True)
    except (GitOpError, ValueError) as exc:
        await query.answer(_one_line(exc), show_alert=True)
        return
    gstate.disarm(user_id, thread_id)
    await query.answer(f"✅ {name}")
    await _safe_edit(query, f"✅ Created and checked out `{name}`.")


# ── commit + push ─────────────────────────────────────────────────────────────


async def _arm_commit(
    query: Any, user_id: int, thread_id: int, window_id: str, repo: str
) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    gstate.arm(
        user_id,
        thread_id,
        gstate.ComposerInput(awaiting="commit_message", window_id=window_id, repo=repo),
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✨ Auto message", callback_data=f"{_CB_PREFIX}commit_auto"
                )
            ],
            [InlineKeyboardButton("✖ Cancel", callback_data=f"{_CB_PREFIX}cancel")],
        ]
    )
    await query.answer()
    await _safe_edit(
        query, "💾 Send a commit message, or tap *Auto message*.", reply_markup=kb
    )


async def _auto_commit_message(repo: str) -> str:
    # Lazy: layer module deferred to the call path.
    from .git_ops import working_tree_status

    status = await asyncio.to_thread(working_tree_status, repo)
    total = status.staged + status.unstaged + status.untracked
    noun = "file" if total == 1 else "files"
    return f"chore: update {total} {noun}"


async def _commit_and_push(query: Any, repo: str, *, message: str | None) -> None:
    # Lazy: layer module deferred to the call path.
    from .git_ops import (
        GitOpError,
        NothingToCommit,
        PushRejected,
        commit_all,
        push_branch,
    )

    msg = message or await _auto_commit_message(repo)
    try:
        sha = await asyncio.to_thread(commit_all, repo, msg)
    except NothingToCommit:
        await query.answer("Nothing to commit", show_alert=True)
        return
    except (GitOpError, ValueError) as exc:
        await query.answer(_one_line(exc), show_alert=True)
        return

    try:
        await asyncio.to_thread(push_branch, repo, set_upstream=True)
    except PushRejected as exc:
        await _safe_edit(query, f"✅ Committed `{sha[:7]}`.\n⬆️ {_one_line(exc)}")
        await query.answer()
        return
    except GitOpError as exc:
        await _safe_edit(
            query, f"✅ Committed `{sha[:7]}`.\n❌ Push failed: {_one_line(exc)}"
        )
        await query.answer()
        return
    await query.answer("✅ Committed & pushed")
    await _safe_edit(query, f"✅ Committed `{sha[:7]}` and pushed.")


async def _push(query: Any, repo: str) -> None:
    # Lazy: layer module deferred to the call path.
    from .git_ops import GitOpError, PushRejected, push_branch

    try:
        await asyncio.to_thread(push_branch, repo, set_upstream=True)
    except PushRejected as exc:
        await query.answer(_one_line(exc), show_alert=True)
        return
    except GitOpError as exc:
        await query.answer(f"Push failed: {_one_line(exc)}", show_alert=True)
        return
    await query.answer("✅ Pushed")
    await _safe_edit(query, "⬆️ Pushed.")


# ── pull request ──────────────────────────────────────────────────────────────


async def _start_pr(
    query: Any, user_id: int, thread_id: int, window_id: str, repo: str
) -> None:
    # Lazy: layer module deferred to the call path.
    from .git_ops import (
        GitOpError,
        PRValidationError,
        current_branch,
        list_branches,
        preflight_pull_request,
    )

    try:
        head = await asyncio.to_thread(current_branch, repo)
        branches = [b.name for b in await asyncio.to_thread(list_branches, repo)]
        base = _default_base(repo, branches)
        await asyncio.to_thread(preflight_pull_request, repo, base=base, head=head)
    except PRValidationError as exc:
        await query.answer(_one_line(exc), show_alert=True)
        return
    except (GitOpError, OSError) as exc:
        await query.answer(_one_line(exc), show_alert=True)
        return

    base_choices = [b for b in branches if b != head] or [base]
    base_idx = base_choices.index(base) if base in base_choices else 0
    gstate.arm(
        user_id,
        thread_id,
        gstate.ComposerInput(
            awaiting="pr_title",
            window_id=window_id,
            repo=repo,
            base=base_choices[base_idx],
            base_choices=base_choices,
            base_idx=base_idx,
        ),
    )
    await query.answer()
    await _safe_edit(
        query, f"🔀 Open PR from `{head}` → `{base}`.\n\nSend the PR title."
    )


async def _show_pr_confirm(query_or_msg: Any, pending: gstate.ComposerInput) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.constants import ParseMode

    draft = "on" if pending.draft else "off"
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"⎇ Base: {pending.base}", callback_data=f"{_CB_PREFIX}pr_base"
                )
            ],
            [
                InlineKeyboardButton(
                    f"📝 Draft: {draft}", callback_data=f"{_CB_PREFIX}pr_draft"
                )
            ],
            [InlineKeyboardButton("✅ Create PR", callback_data=f"{_CB_PREFIX}pr_ok")],
            [InlineKeyboardButton("✖ Cancel", callback_data=f"{_CB_PREFIX}cancel")],
        ]
    )
    text = f"🔀 *Open PR*\n\n*Title:* {pending.pr_title}\n*Base:* `{pending.base}`\n*Draft:* {draft}"
    await query_or_msg.reply_text(
        text=text,
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
        message_thread_id=getattr(query_or_msg, "message_thread_id", None),
    )


async def _cycle_base(query: Any, user_id: int, thread_id: int) -> None:
    pending = gstate.peek(user_id, thread_id)
    if pending is None or not pending.base_choices:
        await query.answer("Expired — reopen Git menu", show_alert=True)
        return
    pending.base_idx = (pending.base_idx + 1) % len(pending.base_choices)
    pending.base = pending.base_choices[pending.base_idx]
    await query.answer(f"Base → {pending.base}")
    await _edit_pr_confirm(query, pending)


async def _toggle_draft(query: Any, user_id: int, thread_id: int) -> None:
    pending = gstate.peek(user_id, thread_id)
    if pending is None:
        await query.answer("Expired — reopen Git menu", show_alert=True)
        return
    pending.draft = not pending.draft
    await query.answer()
    await _edit_pr_confirm(query, pending)


async def _edit_pr_confirm(query: Any, pending: gstate.ComposerInput) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.constants import ParseMode

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.error import TelegramError

    draft = "on" if pending.draft else "off"
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"⎇ Base: {pending.base}", callback_data=f"{_CB_PREFIX}pr_base"
                )
            ],
            [
                InlineKeyboardButton(
                    f"📝 Draft: {draft}", callback_data=f"{_CB_PREFIX}pr_draft"
                )
            ],
            [InlineKeyboardButton("✅ Create PR", callback_data=f"{_CB_PREFIX}pr_ok")],
            [InlineKeyboardButton("✖ Cancel", callback_data=f"{_CB_PREFIX}cancel")],
        ]
    )
    text = f"🔀 *Open PR*\n\n*Title:* {pending.pr_title}\n*Base:* `{pending.base}`\n*Draft:* {draft}"
    try:
        await query.edit_message_text(
            text=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN
        )
    except TelegramError as exc:
        logger.debug("pr confirm re-render no-op: %s", exc)


async def _create_pr(query: Any, user_id: int, thread_id: int, repo: str) -> None:
    # Lazy: layer module deferred to the call path.
    from .git_ops import (
        GitOpError,
        PRValidationError,
        PullRequestError,
        create_pull_request,
        current_branch,
        preflight_pull_request,
    )

    pending = gstate.peek(user_id, thread_id)
    if pending is None or not pending.pr_title:
        await query.answer("Expired — reopen Git menu", show_alert=True)
        return
    try:
        head = await asyncio.to_thread(current_branch, repo)
        await asyncio.to_thread(
            preflight_pull_request, repo, base=pending.base, head=head
        )
        url = await asyncio.to_thread(
            create_pull_request,
            repo,
            title=pending.pr_title,
            body=pending.pr_body,
            base=pending.base,
            head=head,
            draft=pending.draft,
        )
    except (PRValidationError, PullRequestError, GitOpError) as exc:
        await query.answer(_one_line(exc), show_alert=True)
        return
    gstate.disarm(user_id, thread_id)
    await query.answer("✅ PR opened")
    await _safe_edit(query, f"🔀 PR opened: {url}")


async def _open_web_composer(query: Any, window_id: str) -> None:
    """Mint a short-lived compose token and hand the user the web URL."""
    # Lazy: config is the source of the bot token.
    from ccgram.config import config

    # Lazy: layer module deferred to the call path.
    from .share.links import make_compose_url

    url = make_compose_url(bot_token=config.telegram_bot_token, window_id=window_id)
    if not url:
        await query.answer(
            "Web composer unavailable (no Mini App URL set)", show_alert=True
        )
        return
    await query.answer()
    await _safe_edit(query, f"🌐 Open the PR composer (expires in 10 min):\n{url}")


async def _close(query: Any) -> None:
    # Lazy: only needed in this branch.
    import contextlib

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.error import TelegramError

    with contextlib.suppress(TelegramError):
        await query.delete_message()


# ── text capture (branch name / commit message / PR title / PR body) ─────────


async def capture_composer_text(update: Any, _context: Any) -> None:
    """High-priority text handler: consume composer replies before forwarding."""
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import ApplicationHandlerStop

    message = update.message
    if message is None or not message.text:
        return
    user_id, thread_id = _topic(update)
    pending = gstate.peek(user_id, thread_id)
    if pending is None:
        return  # not armed — let normal text handling proceed
    try:
        await _handle_text_reply(message, user_id, thread_id, pending)
    finally:
        raise ApplicationHandlerStop


async def _handle_text_reply(
    message: Any, user_id: int, thread_id: int, pending: gstate.ComposerInput
) -> None:
    text = message.text.strip()
    if pending.awaiting == "branch_name":
        # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
        from ccgram.handlers.topics.worktree import validate_branch_name

        if not await asyncio.to_thread(validate_branch_name, text):
            await message.reply_text(
                "❌ Invalid branch name; send another or reopen Git."
            )
            return
        await _create_branch_from_text(message, user_id, thread_id, pending, text)
    elif pending.awaiting == "commit_message":
        gstate.disarm(user_id, thread_id)
        await _commit_and_push_msg(message, pending.repo, text)
    elif pending.awaiting == "pr_title":
        pending.pr_title = text[:256]
        pending.awaiting = "pr_body"
        gstate.arm(user_id, thread_id, pending)
        await message.reply_text("Send the PR body, or send `-` to skip.")
    elif pending.awaiting == "pr_body":
        pending.pr_body = "" if text == "-" else text
        pending.awaiting = "pr_confirm"
        gstate.arm(user_id, thread_id, pending)
        await _show_pr_confirm(message, pending)


async def _create_branch_from_text(
    message: Any, user_id: int, thread_id: int, pending: gstate.ComposerInput, name: str
) -> None:
    # Lazy: layer module deferred to the call path.
    from .git_ops import GitOpError, create_branch

    try:
        await asyncio.to_thread(create_branch, pending.repo, name, checkout=True)
    except (GitOpError, ValueError) as exc:
        await message.reply_text(f"❌ {_one_line(exc)}")
        return
    gstate.disarm(user_id, thread_id)
    await message.reply_text(f"✅ Created and checked out `{name}`.")


async def _commit_and_push_msg(message: Any, repo: str, msg: str) -> None:
    # Lazy: layer module deferred to the call path.
    from .git_ops import (
        GitOpError,
        NothingToCommit,
        PushRejected,
        commit_all,
        push_branch,
    )

    try:
        sha = await asyncio.to_thread(commit_all, repo, msg)
    except NothingToCommit:
        await message.reply_text("Nothing to commit.")
        return
    except (GitOpError, ValueError) as exc:
        await message.reply_text(f"❌ {_one_line(exc)}")
        return
    try:
        await asyncio.to_thread(push_branch, repo, set_upstream=True)
    except (PushRejected, GitOpError) as exc:
        await message.reply_text(f"✅ Committed `{sha[:7]}`.\n⬆️ {_one_line(exc)}")
        return
    await message.reply_text(f"✅ Committed `{sha[:7]}` and pushed.")


# ── install ──────────────────────────────────────────────────────────────────


def install_git_composer(application: Any) -> None:
    global _installed
    if _installed:
        return
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters

    application.add_handler(
        CallbackQueryHandler(handle_git_callback, pattern=r"^ccgrampro:git:"),
        group=-10,
    )
    # group=-11 so an armed composer reply is consumed before new-session /
    # batcher / forward handling runs.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, capture_composer_text),
        group=-11,
    )
    _installed = True
    logger.info("ccgram-pro git composer installed — branch/commit/push/PR")


def _reset_for_testing() -> None:
    global _installed
    _installed = False
    gstate._reset_for_testing()

"""``/pr-fix <PR#>`` — drive a PR through Cursor-bot review with Claude.

Implementation note: rather than re-implementing the polling +
analyse-fix-commit loop in Python, the command sends Claude a
*structured prompt* describing the loop (taken from the user's
original spec) plus the PR number. Claude already has ``bash`` access,
``gh`` on $PATH, and the polling tools it needs. Each iteration's
output goes through the layer's silencer + summarizer, so the user
sees one final "Done — checks green / max iterations reached"
summary at the end with a link to the full transcript.

``/pr-log <PR#>`` tails the per-PR log Claude writes during the loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..config import pr_loop_log_dir

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

_PROMPT_TEMPLATE = """\
You are entering "PR review fix" mode. Your goal is to bring PR #{pr_number} to a fully-green state where every check passes and every reviewer comment has been addressed.

WORKFLOW:
1. Call `gh pr view {pr_number} --json number,title,headRefName,baseRefName,statusCheckRollup` to read the PR state.
2. Check out the PR branch locally with `gh pr checkout {pr_number}`.
3. Run `gh pr checks {pr_number}` to see CI status. If any checks are failing:
   - Read the failing job logs with `gh run view --log-failed`.
   - Run the equivalent command locally (pnpm typecheck / pnpm test / etc.) and fix the root cause.
4. Read unresolved review comments (Cursor-bot, human reviewers) via `gh api repos/OWNER/REPO/pulls/{pr_number}/comments`.
5. For each comment, analyze deeply:
   - If it's a real issue → fix carefully (production-ready code, no quick hacks), then post a reply via `gh api repos/.../pulls/comments/<id>/replies` confirming.
   - If it's a false positive → reply explaining why and resolve.
6. Commit + push your fixes with a clear conventional-commit message.
7. Wait ~30s, then re-run step 1 to see what's new.
8. Repeat 1-7 until everything is green AND every comment is resolved, with a hard cap of 20 iterations.

CONSTRAINTS:
- Never break existing functionality. Production-ready bar.
- Never merge the PR yourself.
- If at any point a comment is critical and would require reworking the requirements, STOP and surface a question to the user instead of guessing.
- Keep a running log of each iteration at: `{log_path}`. After each iteration append a short header summarizing what changed.
- After reaching a green state OR the 20-iteration cap, post a final short summary.

Begin now with iteration 1. PR number: {pr_number}.
"""


def _log_path(pr_number: int) -> Path:
    return pr_loop_log_dir() / f"pr-{pr_number}.log"


async def pr_fix_command(
    update: "Update", context: "ContextTypes.DEFAULT_TYPE"
) -> None:
    """Trigger the PR review loop on the bound Claude window."""
    # Lazy: avoid an import of the ccgram internal package at module load.
    from ccgram.handlers.callback_helpers import get_thread_id

    # Lazy: ccgram internal — deferred to avoid an import cycle with ccgram bootstrap (the layer is imported during bootstrap).
    from ccgram.handlers.text.text_handler import _forward_message

    # Lazy: ccgram internal — deferred to avoid an import cycle with ccgram bootstrap (the layer is imported during bootstrap).
    from ccgram.telegram_client import PTBTelegramClient

    # Lazy: ccgram internal — deferred to avoid an import cycle with ccgram bootstrap (the layer is imported during bootstrap).
    from ccgram.thread_router import thread_router

    if update.message is None or update.effective_user is None:
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "_Usage:_ `/pr-fix <PR_number>`", parse_mode="Markdown"
        )
        return
    try:
        pr_number = int(args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text(
            f"_`{args[0]}` is not a PR number._", parse_mode="Markdown"
        )
        return

    thread_id = get_thread_id(update)
    user_id = update.effective_user.id
    window_id = thread_router.resolve_window_for_thread(user_id, thread_id or 0)
    if not window_id:
        await update.message.reply_text(
            "_Bind a Claude session in this topic first (send any message)._",
            parse_mode="Markdown",
        )
        return

    log_path = _log_path(pr_number)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)
    prompt = _PROMPT_TEMPLATE.format(pr_number=pr_number, log_path=log_path)

    client = PTBTelegramClient(context.bot)
    await _forward_message(
        window_id,
        user_id,
        thread_id or 0,
        prompt,
        client,
        update.message,
    )
    logger.info(
        "pr-fix dispatched: PR #%d to window %s (log=%s)",
        pr_number,
        window_id,
        log_path,
    )
    await update.message.reply_text(
        f"🔁 PR #{pr_number} review loop started. Final summary lands when done. "
        f"Tail progress with `/pr-log {pr_number}`.",
        parse_mode="Markdown",
    )


async def pr_log_command(
    update: "Update", context: "ContextTypes.DEFAULT_TYPE"
) -> None:
    """Tail the last 80 lines of the PR-fix iteration log."""
    del context

    if update.message is None:
        return
    args = update.message.text.split() if update.message.text else []
    if len(args) < 2:
        await update.message.reply_text(
            "_Usage:_ `/pr-log <PR_number>`", parse_mode="Markdown"
        )
        return
    try:
        pr_number = int(args[1].lstrip("#"))
    except ValueError:
        await update.message.reply_text("_Invalid PR number._", parse_mode="Markdown")
        return

    path = _log_path(pr_number)
    if not path.is_file():
        await update.message.reply_text(
            f"_No log for PR #{pr_number}. Has `/pr-fix {pr_number}` been run?_",
            parse_mode="Markdown",
        )
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        await update.message.reply_text(
            f"_Log read failed: {exc}_", parse_mode="Markdown"
        )
        return
    tail = "\n".join(text.splitlines()[-80:])
    if not tail.strip():
        tail = "(empty)"
    # Telegram caps at 4096 chars; the layer's silencer doesn't gate /pr-log.
    if len(tail) > 3800:
        tail = "…\n" + tail[-3800:]
    await update.message.reply_text(
        f"```\n{tail}\n```",
        parse_mode="MarkdownV2" if False else "Markdown",
    )


__all__ = ["pr_fix_command", "pr_log_command"]

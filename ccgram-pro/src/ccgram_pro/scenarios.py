"""Scenario launcher — one-tap, pre-baked prompts forwarded to the agent.

A 🎬 button rides the Stop-summary action row (just before ⚙️ Settings). Tapping
it opens a small menu of "scenarios": canned, multi-line prompts the user would
otherwise paste by hand. Picking one (a) edits the menu message into a short
"Scenario triggered" record kept in the chat history and (b) forwards the full
prompt to the bound agent exactly as a normal user turn — bypassing the batcher,
via the original (pre-wrap) forward captured by :mod:`input_pipeline.intercept`
— then starts the live progress bubble.

Two scenarios ship today:

- **Self-review** (every session): a deep self-review checklist over the last,
  unpushed changes.
- **PR auto-fixer** (humanprogram backend/app only): drives the repo's
  ``var/pr-check.sh`` loop to address CI + Cursor-bot feedback until the PR is
  green. It needs one input — the PR number — collected through a free-text
  reply (mirrors the voice-edit flow: a high-priority ``MessageHandler`` in
  group −12 consumes the next message in that topic before ccgram's text
  handler can forward it).

The PR-fixer is gated on the session's git ``origin`` remote resolving to a
known humanprogram repository, so the ``REPO`` env (``backend`` →
``primer_server``, ``frontend`` → ``hyper_school_dashboard``) is unambiguous and
the user only has to supply the number.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from . import state

logger = structlog.get_logger()

_CB_PREFIX = "ccgrampro:scn:"

# Layer-local user_data key holding the pending PR-number context:
# {chat_id, thread_id, window_id, repo, prompt_msg_id, user_id}.
AWAITING_PR_NUMBER = "_ccgrampro_awaiting_pr_number"

# Single-shot install guard.
_installed = False

# git origin remote ``owner/repo`` segment → REPO env value pr-check.sh expects.
# Matching the full ``humanprogram/<repo>`` segment (works for SSH, HTTPS, and
# host-alias remote URLs) gates the PR auto-fixer to EXACTLY these two repos —
# any other repository (no match → None) hides the option entirely, and a repo
# that merely shares a name under a different owner won't false-positive.
_REPO_BY_REMOTE: tuple[tuple[str, str], ...] = (
    ("humanprogram/primer_server", "backend"),
    ("humanprogram/hyper_school_dashboard", "frontend"),
)


# ── scenario prompts ──────────────────────────────────────────────────────────

_SELF_REVIEW_PROMPT = """\
Now you need to do careful and deep code review for your last implemented not pushed changes.
We need to make sure that the implementation has no issues and no gaps. Nothing is missing; code is professional, production-ready, follows best practices, and our current project rules.
No need to run any workflows; however, just read again your changes and do a careful self-review.
Check:
- Does the solution fully satisfy the requirements?
- Did it solve the root cause, not just a symptom?
- Is the fix at the correct architectural layer?
- Is it simple enough and not over-engineered?
- Are edge cases handled?
- Are errors handled correctly?
- Are types/contracts/JSDocs or Typedocs updated?
- Are all affected call sites updated?
- Are tests meaningful (Backend only)?
- Is it secure?
- Is performance acceptable?
- Is backward compatibility preserved where needed?
- Are there no TODOs, debug logs, dead code, or temporary artifacts?
- Are we sure we have no regressions?

If you find issues, fix them before finalizing."""

# ``__PR__`` / ``__REPO__`` are substituted via str.replace (no brace escaping,
# the body contains literal JSON braces).
_PR_FIXER_TEMPLATE = """\
We are entering mode to address code review feedback and make PR #__PR__ fully ready for merge. You need to use the following script:

Script: /root/projects/humanprogram/backend/var/pr-check.sh
Env:    REPO=__REPO__   (required — use this value)
        OWNER=humanprogram   (default)
        POLL_SECS=30   (default)

Commands:
  REPO=__REPO__ /root/projects/humanprogram/backend/var/pr-check.sh status __PR__
      Polls until no check is in_progress (30s intervals), then prints JSON:
        {
          rollup,
          checks: [{name, status, conclusion, url}],
          cursor_comments: [{id, path, line, url, title, severity, description, locations[]}]
        }
      Only unresolved cursor-bot threads. If cursor check failed with no
      comments yet, waits 30s once more before returning.

  REPO=__REPO__ /root/projects/humanprogram/backend/var/pr-check.sh reply __PR__ <comment-id> "<text>"
      Posts a threaded reply. comment-id = `id` from cursor_comments.

  REPO=__REPO__ /root/projects/humanprogram/backend/var/pr-check.sh resolve __PR__ <comment-id>
      Marks the thread containing that comment as resolved.

  REPO=__REPO__ /root/projects/humanprogram/backend/var/pr-check.sh reply-resolve __PR__ <comment-id> "<text>"
      Reply, then resolve, in one call.

Status JSON goes to stdout; progress messages go to stderr.

No need to limit script output length or execution time. This script can run for some time (Cursor Bot review can take 5-20 minutes) until we have PR status ready for your review. If everything is ready or something is wrong and needs my attention, you just play notification sound so I can check the result or answer your questions - afplay /System/Library/Sounds/Glass.aiff.
However, if you see that something has failed or if we have comments from Cursor bot, you should automatically do analysis and fix the issues:
- If Typecheck is failing, run `pnpm typecheck` locally and fix the issues.
- If Unit tests are failing, run `pnpm test` locally and fix the issues.
- If Cursor bot comments are present, analyze the comments deeply to understand if they are real issues or false positives. Don't make immediate assumptions. Spend time for analysis.
  - If they are real issues, fix them carefully and professionally to make sure nothing breaks and issue is resolved. We need production ready code. (We should be careful with marking issues as real issues, just to make sure that we do not go too far from our initial requirements. If something is critical, requires any discussions and re-work, better to stop the loop and ask me. I just don't want, after all the loops, to check the result and find that some functionality was cut or significantly reworked without my approval.) After that, run `reply-resolve` command to resolve the issue and leave your comment that issue was addressed. Next commit and push changes (with professional commit message, avoid noting Claude as collaborator in the commit message), and run `pr-check.sh` again to wait and check the PR status after your changes. Script has short 5sec delay in the beginning to let Github process your commit and run pipelines.
  - If they are false positives, run `reply-resolve` command to resolve the issue and leave your comment that this issue is false positive. Nothing to commit if no any real issue that requires addressing.
Continue this process until all issues are resolved, all checks are green and PR is ready for merge. After each iteration, provide some short but prominent header with a short summary of the progress - what was done.
Remember to be careful to not break anything. We need a professional and production-ready implementation that will follow best practices and our current project rules. No quick changes or hacks. We should have proper implementation.

One more rule is to avoid an infinite loop. Please execute no more than 20 iterations. Generally Cursor bot leave 1-5 comments after each run, it is not leaving all comments at once, so it is fine to have some amount of iterations. However, after the 20th iteration, please break to avoid an infinite loop.

Don't merge PRs by yourself. Your only goal is to bring it to the state where all checks are green."""


def _pr_fixer_prompt(pr: str, repo: str) -> str:
    return _PR_FIXER_TEMPLATE.replace("__PR__", pr).replace("__REPO__", repo)


_COMMIT_PUSH_PROMPT = """\
Please commit the current changes and push them.

- First review what actually changed (git status + git diff). Stage only the files that belong in this change — do NOT blindly `git add -A`. Leave out anything that looks unintended or unrelated (build artifacts, local scratch/config, editor files, files outside the scope of recent work); if you spot such a file, leave it unstaged and tell me about it.
- Write a clear, meaningful commit message describing the INTENT of the change (not a file list), following this project's existing commit-message conventions (use Conventional Commits if that's the style here).
- Do NOT add a `Co-Authored-By` trailer and do NOT mention Claude, AI, or this assistant anywhere in the commit message.
- Then push to the current branch's upstream (set the upstream if it doesn't exist yet).
- If there is nothing staged to commit but there are unpushed commits, just push them.
- If there is genuinely nothing to commit or push, say so. If anything is ambiguous or risky (unrelated changes mixed together, a force-push would be needed, detached HEAD, conflicts), STOP and ask me instead of guessing.

When done, report the exact commit message you used and the push result (branch + remote)."""


# ── callback codec (window_id is the trailing, colon-safe field) ───────────────


def _encode(action: str, window_id: str) -> str:
    return f"{_CB_PREFIX}{action}:{window_id}"


def _decode(data: str) -> tuple[str, str] | None:
    """Return (action, window_id) or None. window_id may contain ``:``."""
    if not data.startswith(_CB_PREFIX):
        return None
    action, _, window_id = data[len(_CB_PREFIX) :].partition(":")
    if action not in ("menu", "sr", "cp", "pr", "x") or not window_id:
        return None
    return action, window_id


def scenarios_button_for_window(window_id: str) -> Any:
    """The 🎬 action-row button that opens the scenarios menu."""
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton

    return InlineKeyboardButton("🎬", callback_data=_encode("menu", window_id))


# ── repo / eligibility ─────────────────────────────────────────────────────────


async def _run_git(repo_path: str, *args: str) -> str | None:
    """Run ``git -C <repo_path> <args>`` and return trimmed stdout, or None.

    Bounded by a 5s timeout (a wedged git must never hang a menu open).
    """
    # Lazy: only needed on this path.
    import contextlib

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            repo_path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError, ValueError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except TimeoutError, OSError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return None
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", "replace").strip() or None


async def _git_remote_url(repo_path: str) -> str | None:
    return await _run_git(repo_path, "remote", "get-url", "origin")


async def _is_git_repo(window_id: str) -> bool:
    """True when the session's resolved directory is inside a git work tree."""
    repo_path = state.resolve_repo(window_id)
    if not repo_path:
        return False
    return await _run_git(repo_path, "rev-parse", "--is-inside-work-tree") == "true"


async def _detect_pr_repo(window_id: str) -> str | None:
    """Resolve the ``REPO`` env value for the PR-fixer, or None if ineligible.

    Keyed off the git ``origin`` remote so it works for worktrees and clones
    too (not just the canonical project path).
    """
    repo_path = state.resolve_repo(window_id)
    if not repo_path:
        return None
    url = await _git_remote_url(repo_path)
    if not url:
        return None
    for needle, repo in _REPO_BY_REMOTE:
        if needle in url:
            return repo
    return None


# ── shared forward ─────────────────────────────────────────────────────────────


async def _forward_scenario(
    *,
    window_id: str,
    user_id: int,
    thread_id: int,
    prompt: str,
    anchor: Any,
    bot: Any,
) -> None:
    """Forward *prompt* to the agent as a normal turn, then start the bubble.

    Routes through the ORIGINAL (pre-batch-wrap) forward captured by
    :mod:`input_pipeline.intercept`, so a scenario fires immediately as a turn
    instead of being appended to the batch.
    """
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.telegram_client import PTBTelegramClient

    # Lazy: deferred to avoid a heavy/cyclic import at module load.
    from .input_pipeline import intercept

    original = intercept._ORIGINAL_FORWARD_MESSAGE
    if original is None:
        logger.warning("scenario forward skipped — original forward not wired")
        return
    client = PTBTelegramClient(bot)
    try:
        await original(window_id, user_id, thread_id, prompt, client, anchor)
    except Exception:  # noqa: BLE001 -- never let a scenario crash the handler
        logger.exception("scenario forward failed for %s", window_id)
        return

    # Live "⚙️ Working on your request…" bubble (mirrors the batch-flush path).
    # Lazy: deferred to avoid a heavy/cyclic import at module load.
    from .config import load_settings

    # Lazy: deferred to avoid a heavy/cyclic import at module load.
    from .input_pipeline.silencer_guard import is_silent_for_window

    # Lazy: deferred to avoid a heavy/cyclic import at module load.
    from .output_pipeline import progress_bubble

    chat_id = getattr(getattr(anchor, "chat", None), "id", None)
    if (
        chat_id is not None
        and load_settings().defaults.progress_bubble
        and is_silent_for_window(window_id)
    ):
        await progress_bubble.start_bubble(
            window_id=window_id,
            bot=bot,
            chat_id=chat_id,
            thread_id=thread_id,
            transcript_path=intercept._resolve_transcript_path(window_id),
        )


async def _edit_to_note(message: Any, text: str) -> None:
    # Lazy: only needed on this path.
    import contextlib

    # Lazy: PTB error type only needed here.
    from telegram.error import TelegramError

    with contextlib.suppress(TelegramError):
        await message.edit_text(text=text, reply_markup=None)


# ── callbacks ───────────────────────────────────────────────────────────────────


async def handle_scenarios_callback(update: Any, context: Any) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import ApplicationHandlerStop

    try:
        await _dispatch(update, context)
    except Exception:  # noqa: BLE001 -- log, then stop the handler chain below
        logger.exception("scenarios callback failed")
    finally:
        raise ApplicationHandlerStop


async def _dispatch(update: Any, context: Any) -> None:
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.callback_helpers import get_thread_id, user_owns_window

    query = update.callback_query
    if query is None or not query.data:
        return
    decoded = _decode(query.data)
    if decoded is None:
        await query.answer("Invalid", show_alert=True)
        return
    action, window_id = decoded

    user = update.effective_user
    user_id = user.id if user else 0
    if not user_owns_window(user_id, window_id):
        await query.answer("Not your session", show_alert=True)
        return

    if action == "x":
        await _cancel(query, context)
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        await query.answer("No topic context", show_alert=True)
        return
    if query.message is None:
        await query.answer("This card expired — reopen the menu.", show_alert=True)
        return

    await _route_topic_action(query, action, window_id, user_id, thread_id, context)


async def _route_topic_action(
    query: Any,
    action: str,
    window_id: str,
    user_id: int,
    thread_id: int,
    context: Any,
) -> None:
    """Dispatch a validated, topic-scoped scenario action to its handler."""
    if action == "menu":
        await _open_menu(query, window_id)
    elif action == "sr":
        await _run_self_review(query, window_id, user_id, thread_id, context)
    elif action == "cp":
        await _run_commit_push(query, window_id, user_id, thread_id, context)
    elif action == "pr":
        await _ask_pr_number(query, window_id, user_id, thread_id, context)


async def _open_menu(query: Any, window_id: str) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Lazy: only needed on this path.
    import contextlib

    # Lazy: PTB error type only needed here.
    from telegram.error import TelegramError

    rows = [
        [InlineKeyboardButton("🔎 Self-review", callback_data=_encode("sr", window_id))]
    ]
    if await _is_git_repo(window_id):
        rows.append(
            [
                InlineKeyboardButton(
                    "💾 Commit & push", callback_data=_encode("cp", window_id)
                )
            ]
        )
    if await _detect_pr_repo(window_id) is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    "🤖 PR auto-fixer", callback_data=_encode("pr", window_id)
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("✖ Cancel", callback_data=_encode("x", window_id))]
    )
    await query.answer()
    msg = query.message
    if msg is None:
        return
    thread_id = getattr(msg, "message_thread_id", None)
    with contextlib.suppress(TelegramError):
        await msg.reply_text(
            text="🎬 Scenarios — pick one to run:",
            reply_markup=InlineKeyboardMarkup(rows),
            message_thread_id=thread_id,
        )


async def _run_self_review(
    query: Any, window_id: str, user_id: int, thread_id: int, context: Any
) -> None:
    note = (
        "🔎 Scenario triggered: Self-review\n"
        "Deep self-review of the last unpushed changes — fixing any issues found."
    )
    await _edit_to_note(query.message, note)
    await _forward_scenario(
        window_id=window_id,
        user_id=user_id,
        thread_id=thread_id,
        prompt=_SELF_REVIEW_PROMPT,
        anchor=query.message,
        bot=context.bot,
    )
    await query.answer("Self-review started")


async def _run_commit_push(
    query: Any, window_id: str, user_id: int, thread_id: int, context: Any
) -> None:
    note = (
        "💾 Scenario triggered: Commit & push\n"
        "Claude is reviewing the changes, writing a commit message, and pushing."
    )
    await _edit_to_note(query.message, note)
    await _forward_scenario(
        window_id=window_id,
        user_id=user_id,
        thread_id=thread_id,
        prompt=_COMMIT_PUSH_PROMPT,
        anchor=query.message,
        bot=context.bot,
    )
    await query.answer("Commit & push started")


async def _ask_pr_number(
    query: Any, window_id: str, user_id: int, thread_id: int, context: Any
) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Lazy: only needed on this path.
    import contextlib

    # Lazy: PTB error type only needed here.
    from telegram.error import TelegramError

    repo = await _detect_pr_repo(window_id)
    if repo is None:
        await query.answer("Not a humanprogram backend/app repo", show_alert=True)
        return
    msg = query.message
    if msg is None:
        return
    if context.user_data is not None:
        context.user_data[AWAITING_PR_NUMBER] = {
            "chat_id": msg.chat.id,
            "thread_id": thread_id,
            "window_id": window_id,
            "repo": repo,
            "prompt_msg_id": msg.message_id,
            "user_id": user_id,
        }
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✖ Cancel", callback_data=_encode("x", window_id))]]
    )
    with contextlib.suppress(TelegramError):
        await msg.edit_text(
            text=f"🤖 PR auto-fixer ({repo}) — reply with the PR number (e.g. 1234).",
            reply_markup=keyboard,
        )
    await query.answer("Send the PR number")


async def _cancel(query: Any, context: Any) -> None:
    # Lazy: only needed on this path.
    import contextlib

    # Lazy: PTB error type only needed here.
    from telegram.error import TelegramError

    if context.user_data is not None:
        context.user_data.pop(AWAITING_PR_NUMBER, None)
    if query.message is not None:
        with contextlib.suppress(TelegramError):
            await query.message.delete()
    await query.answer("Cancelled")


async def consume_pr_number_reply(update: Any, context: Any) -> None:
    """Group −12 text handler: consume a PR number when the PR flow is armed.

    Pure pass-through (returns without stopping) when no PR number is awaited,
    so normal messages reach ccgram's text handler untouched.
    """
    pend = context.user_data.get(AWAITING_PR_NUMBER) if context.user_data else None
    if not pend:
        return
    message = update.message
    if message is None or not (message.text and message.text.strip()):
        return

    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.callback_helpers import get_thread_id

    if pend.get("thread_id", 0) != (get_thread_id(update) or 0):
        return  # armed in a different topic — leave it, pass through

    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import ApplicationHandlerStop

    # Lazy: only needed on this path.
    import contextlib

    # Lazy: PTB error type only needed here.
    from telegram.error import TelegramError

    bot = context.bot
    raw = message.text.strip().lstrip("#").strip()
    if not raw.isdigit():
        # Invalid — re-prompt, keep the flow armed, drop the stray reply.
        await _reprompt_invalid(bot, pend)
        with contextlib.suppress(TelegramError):
            await message.delete()
        raise ApplicationHandlerStop

    pr = raw
    repo = pend["repo"]
    window_id = pend["window_id"]
    user_id = pend.get("user_id", 0)
    thread_id = pend["thread_id"]
    if context.user_data is not None:
        context.user_data.pop(AWAITING_PR_NUMBER, None)

    note = (
        "🤖 Scenario triggered: PR auto-fixer\n"
        f"Driving PR #{pr} ({repo}) to green — addressing checks & Cursor "
        "feedback (≤20 iterations)."
    )
    with contextlib.suppress(TelegramError):
        await bot.edit_message_text(
            chat_id=pend["chat_id"], message_id=pend["prompt_msg_id"], text=note
        )
    await _forward_scenario(
        window_id=window_id,
        user_id=user_id,
        thread_id=thread_id,
        prompt=_pr_fixer_prompt(pr, repo),
        anchor=message,
        bot=bot,
    )
    # Keep the chat clean — drop the user's bare-number message.
    with contextlib.suppress(TelegramError):
        await message.delete()
    raise ApplicationHandlerStop


async def _reprompt_invalid(bot: Any, pend: dict[str, Any]) -> None:
    # Lazy: PTB types only needed on the handler/send path.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Lazy: only needed on this path.
    import contextlib

    # Lazy: PTB error type only needed here.
    from telegram.error import TelegramError

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✖ Cancel", callback_data=_encode("x", pend["window_id"])
                )
            ]
        ]
    )
    with contextlib.suppress(TelegramError):
        await bot.edit_message_text(
            chat_id=pend["chat_id"],
            message_id=pend["prompt_msg_id"],
            text=(
                "🤖 PR auto-fixer — that doesn't look like a PR number. "
                "Reply with just the number, e.g. 1234."
            ),
            reply_markup=keyboard,
        )


# ── install ─────────────────────────────────────────────────────────────────────


def install_scenarios(application: Any) -> None:
    """Register the scenarios callback + PR-number text handler on *application*."""
    global _installed
    if _installed:
        return
    # Lazy: PTB types only needed on the handler/send path.
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters

    # group=-10: run before ccgram's catch-all CallbackQueryHandler (group 0),
    # alongside the layer's other -10 handlers (each pattern-gated).
    application.add_handler(
        CallbackQueryHandler(handle_scenarios_callback, pattern=r"^ccgrampro:scn:"),
        group=-10,
    )
    # group=-12: consume a PR-number reply before the voice-edit (-11) and core
    # text (0) handlers. No-op pass-through when no PR number is awaited.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, consume_pr_number_reply),
        group=-12,
    )
    _installed = True
    logger.info("ccgram-pro scenarios installed — self-review + PR auto-fixer")


def _reset_for_testing() -> None:
    global _installed
    _installed = False

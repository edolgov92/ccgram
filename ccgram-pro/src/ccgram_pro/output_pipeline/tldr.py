"""The Claude-produced TL;DR contract — sentinel, system prompt, extract/strip.

Single source of truth shared by:

- the launch override (``new_session._apply_overrides``), which appends
  :data:`TLDR_SYSTEM_PROMPT` to Claude's system prompt so Claude itself emits
  a short, user-friendly summary at the end of substantive responses;
- the Stop summarizer, which extracts that block to post to Telegram;
- the web transcript builder, which strips the block so the "full chat" view
  shows only the technical detail (the TL;DR is a Telegram-only courtesy).

The sentinel is an HTML comment pair: invisible in rendered Markdown, safe in
Telegram and inside JSONL ``text`` blocks, and trivially regex-matched.
"""

from __future__ import annotations

import re

TLDR_OPEN = "<!--ccgram:tldr-->"
TLDR_CLOSE = "<!--/ccgram:tldr-->"

_TLDR_RE = re.compile(
    re.escape(TLDR_OPEN) + r"(.*?)" + re.escape(TLDR_CLOSE), re.DOTALL
)

TLDR_SYSTEM_PROMPT = (
    "At the very end of a substantive response (one where you did real work — "
    "edited files, ran commands, investigated something, or answered a "
    "non-trivial question), append a short summary for a non-technical teammate "
    "reading on their phone, wrapped EXACTLY in these markers on their own "
    f"lines:\n{TLDR_OPEN}\n"
    "1-4 plain-language sentences (or a few short bullets) on what you did or "
    "found and what it means. No code, no file paths unless essential.\n"
    f"{TLDR_CLOSE}\n"
    "Put the full technical detail ABOVE the markers as usual. Emit this block "
    "at most once, as the last thing in your message. Omit it entirely for "
    "trivial replies such as greetings, one-line answers, or clarifying "
    "questions."
)


PROGRESS_OPEN = "<!--ccgram:progress-->"
PROGRESS_CLOSE = "<!--/ccgram:progress-->"

_PROGRESS_RE = re.compile(
    re.escape(PROGRESS_OPEN) + r"(.*?)" + re.escape(PROGRESS_CLOSE), re.DOTALL
)

PROGRESS_SYSTEM_PROMPT = (
    "LIVE PROGRESS (REQUIRED — not optional narration): the user watches a live "
    "status indicator while you work, fed by short progress markers you emit. "
    "You MUST keep it updated.\n"
    "Rule: BEFORE each tool call (reading a file, editing, writing, running a "
    "command, searching, fetching, launching a subtask, etc.) and as you START "
    "each new step of the task, first output ONE status line saying what you are "
    "about to do, wrapped EXACTLY in these markers on their own line:\n"
    f"{PROGRESS_OPEN}Reading the auth module{PROGRESS_CLOSE}\n"
    "Keep each line short (3-8 words, under ~80 chars) and present-tense, e.g. "
    "'Mapping the data flow', 'Editing the login handler', 'Running the test "
    "suite', 'Investigating the failure', 'Reviewing the changes'. Emit a fresh "
    "marker for every new step — expect several per turn on any multi-step task. "
    "They are stripped from your rendered reply (they only drive the indicator), "
    "so there is no cost to emitting them and you must never skip them to save "
    "space. The ONLY time you may omit them is a reply that uses no tools at all "
    "(a greeting, a one-line answer, or a clarifying question)."
)

# Appended verbatim to every Claude launch so Claude itself produces both the
# user-facing TL;DR and the live progress notes. Claude concatenates multiple
# ``--append-system-prompt`` tokens, so passing the two combined is equivalent
# to passing them separately — we combine for a single, tidy flag.
LAUNCH_SYSTEM_PROMPT = f"{TLDR_SYSTEM_PROMPT}\n\n{PROGRESS_SYSTEM_PROMPT}"


def extract_progress_lines(text: str) -> list[str]:
    """Return every progress note (markers stripped) from *text*, in order.

    Whitespace-only matches are dropped. Used by the live progress bubble to
    grow its bulleted list as Claude narrates each step.
    """
    return [m.strip() for m in _PROGRESS_RE.findall(text) if m.strip()]


def strip_progress(text: str) -> str:
    """Remove every progress-note block from *text* (chat + web never show them)."""
    return _PROGRESS_RE.sub("", text).rstrip()


def extract_tldr(text: str) -> str | None:
    """Return the TL;DR body (markers stripped) from *text*, or None.

    If multiple blocks are present, the last one wins. Whitespace-only blocks
    are treated as absent.
    """
    matches = _TLDR_RE.findall(text)
    if not matches:
        return None
    body = matches[-1].strip()
    return body or None


def strip_tldr(text: str) -> str:
    """Remove every TL;DR block from *text* (for the web full-chat view)."""
    return _TLDR_RE.sub("", text).rstrip()

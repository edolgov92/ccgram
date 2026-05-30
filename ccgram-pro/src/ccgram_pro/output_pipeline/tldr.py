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

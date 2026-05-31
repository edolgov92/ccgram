"""Condense an ExitPlanMode plan to a 2-3 sentence "main idea" for Telegram.

The full plan markdown is rendered on the web ``/plan`` page; Telegram gets only
the gist plus Approve / Keep-planning buttons. We try the configured text
completer (cheap model) for a clean paraphrase and fall back to a heuristic
(first heading + first paragraph) when no LLM is configured or it errors.
"""

from __future__ import annotations

import asyncio
import re

import structlog

logger = structlog.get_logger()

_PLAN_SUMMARY_TIMEOUT = 5.0
_MAX_IDEA_CHARS = 350
_HEURISTIC_BODY_CHARS = 200

_PLAN_SYSTEM_PROMPT = (
    "You are summarizing a software implementation plan for a developer who will "
    "approve or reject it from their phone. Read the plan and write a 2-3 "
    "sentence plain-English summary of the MAIN IDEA: what will be built or "
    "changed and the overall approach. No preamble, no bullet lists, no "
    "markdown, no restating the heading verbatim. Be concrete but brief. Output "
    "only the summary."
)


def _first_idea(plan_md: str) -> str:
    """Heuristic gist: first heading (if any) + first non-empty paragraph."""
    lines = plan_md.strip().splitlines()
    heading = ""
    body_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if body_lines:
                break
            continue
        if stripped.startswith("#") and not heading:
            heading = stripped.lstrip("#").strip()
            continue
        # Skip list bullets when looking for a prose lead-in.
        body_lines.append(re.sub(r"^[-*+]\s+", "", stripped))
        if len(" ".join(body_lines)) > _HEURISTIC_BODY_CHARS:
            break
    idea = " ".join(part for part in [heading, " ".join(body_lines)] if part).strip()
    idea = re.sub(r"[*`_#]+", "", idea).strip()
    if len(idea) > _MAX_IDEA_CHARS:
        idea = idea[: _MAX_IDEA_CHARS - 1].rstrip() + "…"
    return idea or "Claude has a plan ready for your review."


async def condense_plan(plan_md: str) -> str:
    """Return a short main-idea summary of *plan_md* (LLM, heuristic fallback)."""
    if not plan_md.strip():
        return "Claude has a plan ready for your review."
    # Lazy: llm pulls httpx + config; only needed when a plan arrives.
    from ccgram.llm import get_text_completer

    completer = get_text_completer()
    if completer is None:
        return _first_idea(plan_md)
    try:
        idea = await asyncio.wait_for(
            completer.complete(_PLAN_SYSTEM_PROMPT, plan_md[:8000]),
            timeout=_PLAN_SUMMARY_TIMEOUT,
        )
    except TimeoutError, RuntimeError, OSError, ValueError:
        logger.debug("plan condense failed; using heuristic", exc_info=True)
        return _first_idea(plan_md)
    except Exception:  # noqa: BLE001 -- never break the plan prompt
        logger.debug("plan condense raised; using heuristic", exc_info=True)
        return _first_idea(plan_md)
    idea = idea.strip()
    if not idea or len(idea) > 4 * len(plan_md) + 200:
        return _first_idea(plan_md)
    if len(idea) > _MAX_IDEA_CHARS:
        idea = idea[: _MAX_IDEA_CHARS - 1].rstrip() + "…"
    return idea

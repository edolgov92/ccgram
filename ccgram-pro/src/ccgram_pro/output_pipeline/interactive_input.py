"""Read structured interactive-prompt data from the transcript tail (or pane).

Claude Code's ``AskUserQuestion`` and ``ExitPlanMode`` tools carry their full
input in the JSONL transcript (the ``Notification`` hook only gives us the tool
name). Reading the structured input lets the layer render a clean, semantic
Telegram keyboard — one button per option — instead of screen-scraping the TUI.

We reuse :func:`transcript_events.extract_events` (which preserves ``tool_input``
for ``tool_use`` blocks) and return the most recent UN-answered prompt — a
``tool_use`` whose ``tool_use_id`` has no matching ``tool_result`` yet.

Newer Claude Code builds render some ``AskUserQuestion`` prompts WITHOUT writing
a corresponding ``tool_use`` to the transcript — the question exists only in the
rendered TUI. :func:`parse_pane_prompt` recovers those by parsing the live pane,
gated on ccgram's own UI classifier tagging the region as ``AskUserQuestion`` so
permission prompts / plans / settings are left to their proper handlers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .transcript_events import extract_events


@dataclass(frozen=True)
class AskQuestion:
    """A normalized AskUserQuestion prompt (its first question)."""

    tool_use_id: str
    question: str
    options: list[str]
    multi_select: bool
    total: int = 1


def _answered_ids(events: list) -> set[str]:
    return {
        ev.tool_use_id for ev in events if ev.kind == "tool_result" and ev.tool_use_id
    }


def _parse_ask(
    tool_input: object, tool_use_id: str, total_hint: int
) -> AskQuestion | None:
    if not isinstance(tool_input, dict):
        return None
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    first = questions[0]
    if not isinstance(first, dict):
        return None
    question = str(
        first.get("question") or first.get("header") or "Choose an option"
    ).strip()
    options: list[str] = []
    raw_options = first.get("options")
    if isinstance(raw_options, list):
        for opt in raw_options:
            if isinstance(opt, dict):
                label = str(opt.get("label") or opt.get("value") or "").strip()
            else:
                label = str(opt).strip()
            if label:
                options.append(label)
    if not options:
        return None
    return AskQuestion(
        tool_use_id=tool_use_id,
        question=question,
        options=options,
        multi_select=bool(first.get("multiSelect", False)),
        total=len(questions) if isinstance(questions, list) else total_hint,
    )


def read_active_prompt(transcript_path: str) -> tuple[str, object] | None:
    """Return the CURRENTLY-active interactive prompt as ``(kind, payload)``.

    ``kind`` ∈ {"ask", "plan"}; payload is an :class:`AskQuestion` or the plan
    markdown. Returns ``None`` when there's no live AskUserQuestion / ExitPlanMode
    prompt — crucially, the decision is made on the MOST RECENT ``tool_use`` only,
    so a permission prompt (whose latest tool_use is the gated tool, e.g. Bash)
    yields ``None`` and the caller falls back to ccgram's scraped UI.

    This is the reliable trigger: the ``Notification`` hook reports an empty
    ``tool_name`` for every prompt, so the tool kind must come from the transcript.
    """
    events = extract_events(transcript_path)
    answered = _answered_ids(events)
    for ev in reversed(events):
        if ev.kind != "tool_use":
            continue
        # The newest tool_use decides. If it's already answered, no live prompt.
        if ev.tool_use_id and ev.tool_use_id in answered:
            return None
        if ev.tool_name == "AskUserQuestion":
            parsed = _parse_ask(ev.tool_input, ev.tool_use_id, total_hint=1)
            return ("ask", parsed) if parsed else None
        if ev.tool_name == "ExitPlanMode" and isinstance(ev.tool_input, dict):
            plan = ev.tool_input.get("plan")
            if isinstance(plan, str) and plan.strip():
                return ("plan", plan)
            return None
        # Newest tool_use is some other tool (permission-gated) — not our prompt.
        return None
    return None


# ── Pane fallback: AskUserQuestion that never reaches the transcript ────────

# A numbered option row, optionally cursor-marked (❯ / › / >).
_OPTION_RE = re.compile(r"^\s*[❯›>]?\s*(\d+)[.)]\s+(\S.*)$")
# The question header line begins with a checkbox glyph (single- or multi-tab).
_HEADER_RE = re.compile(r"^\s*←?\s*[☐✔☒▣▢]\s*(.+)$")
# A horizontal rule / blank-ish chrome line to skip while reading the preamble.
_RULE_RE = re.compile(r"^[\s─—\-_·•]*$")
# Footer wording hinting at multi-select (Space toggles rows).
_MULTI_RE = re.compile(r"(?i)\bspace\b.*\b(toggle|select)\b|\btoggle\b")
# A lone numbered line is almost certainly a misparse, not a real choice menu.
_MIN_MENU_OPTIONS = 2


def _parse_menu_block(block: str) -> AskQuestion | None:
    """Parse a numbered selection menu (question + options) from pane text."""
    lines = block.split("\n")
    options: list[str] = []
    first_opt_idx: int | None = None
    for i, line in enumerate(lines):
        m = _OPTION_RE.match(line)
        if m:
            if first_opt_idx is None:
                first_opt_idx = i
            label = m.group(2).strip()
            if label:
                options.append(label)
    # Require a real choice menu — a lone option is almost certainly a misparse.
    if first_opt_idx is None or len(options) < _MIN_MENU_OPTIONS:
        return None

    # Everything above the first option is the prompt text. The checkbox-glyph
    # line is the short header/tab label; the remaining lines are the question
    # body, wrapped by the terminal — rejoin them into one paragraph.
    header_parts: list[str] = []
    body_parts: list[str] = []
    for line in lines[:first_opt_idx]:
        stripped = line.strip()
        if not stripped or _RULE_RE.match(stripped):
            continue
        head = _HEADER_RE.match(stripped)
        if head and head.group(1).strip():
            header_parts.append(head.group(1).strip())
        else:
            body_parts.append(stripped)
    header = " ".join(header_parts).strip()
    body = " ".join(body_parts).strip()
    if header and body:
        question = f"{header}\n\n{body}"
    else:
        question = body or header or "Choose an option"

    return AskQuestion(
        tool_use_id="",
        question=question,
        options=options,
        multi_select=bool(_MULTI_RE.search(block)),
        total=1,
    )


def parse_pane_prompt(pane_text: str) -> tuple[str, object] | None:
    """Recover an active AskUserQuestion straight from the rendered TUI pane.

    Used as a fallback when :func:`read_active_prompt` finds nothing in the
    transcript (newer Claude Code builds don't always record the question as a
    ``tool_use``). To stay safe, we only parse when ccgram's own UI classifier
    tags the region as ``AskUserQuestion`` — permission prompts, plans, settings
    and other selectors classify differently and are left to their handlers.
    """
    if not pane_text or not pane_text.strip():
        return None
    # Lazy: ccgram's terminal parser classifies + isolates the UI region; the
    # top-level import would couple this transcript reader to core's bootstrap.
    from ccgram.terminal_parser import extract_interactive_content

    content = extract_interactive_content(pane_text)
    if content is None or content.name != "AskUserQuestion":
        return None
    parsed = _parse_menu_block(content.content)
    return ("ask", parsed) if parsed else None

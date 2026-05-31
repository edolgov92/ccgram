"""Read structured interactive-prompt data from the transcript tail.

Claude Code's ``AskUserQuestion`` and ``ExitPlanMode`` tools carry their full
input in the JSONL transcript (the ``Notification`` hook only gives us the tool
name). Reading the structured input lets the layer render a clean, semantic
Telegram keyboard — one button per option — instead of screen-scraping the TUI.

We reuse :func:`transcript_events.extract_events` (which preserves ``tool_input``
for ``tool_use`` blocks) and return the most recent UN-answered prompt — a
``tool_use`` whose ``tool_use_id`` has no matching ``tool_result`` yet.
"""

from __future__ import annotations

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

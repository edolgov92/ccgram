"""Parse a Claude Code JSONL transcript into structured turn events.

The transcript viewer needs more than a flat text blob — it needs to
know *what each piece is* (user message, assistant text, thinking,
tool call, tool result) so it can render each with distinct visual
treatment. This module turns the raw JSONL into a typed event list the
HTML renderer consumes.

Each Claude Code transcript line is a JSON object; the relevant ones
carry ``message.content`` as either a string or a list of typed blocks
(``text`` / ``thinking`` / ``tool_use`` / ``tool_result``). ``tool_use``
ids are threaded so a ``tool_result`` can be attached to the call that
produced it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger()


@dataclass
class TurnEvent:
    """One renderable item in the transcript.

    ``kind`` ∈ {"user", "assistant", "thinking", "tool_use",
    "tool_result"}. Fields are populated per-kind; unused ones stay at
    their defaults so the renderer can branch on ``kind`` cleanly.
    """

    kind: str
    text: str = ""
    tool_name: str = ""
    tool_input: dict | None = None
    tool_use_id: str = ""
    is_error: bool = False


def _stringify_tool_result(content: object) -> tuple[str, bool]:
    """Normalize a tool_result content payload to (text, is_error)."""
    if isinstance(content, str):
        return content, False
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "\n".join(parts), False
    return str(content), False


# Read a generous window of recent transcript lines (a long agentic turn
# can produce hundreds of entries), but cap the stored event count so the
# share JSON stays bounded. The view paginates within these events, showing
# the newest page first and loading older ones on scroll.
_MAX_TRANSCRIPT_LINES = 4000
_MAX_STORED_EVENTS = 600


def extract_events(
    transcript_path: str, *, max_lines: int = _MAX_TRANSCRIPT_LINES
) -> list[TurnEvent]:
    """Return structured events for the recent transcript window.

    Reads the last *max_lines* JSONL entries — enough for a long
    multi-message turn while bounding work on a huge transcript. Order is
    preserved (chronological). The result is capped to the newest
    ``_MAX_STORED_EVENTS`` so the share record stays a sane size; the view
    paginates within whatever is stored. ``tool_result`` blocks carry
    their ``tool_use_id`` so the renderer can collapse them under the
    matching call.
    """
    try:
        raw = Path(transcript_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("could not read transcript %s: %s", transcript_path, exc)
        return []

    events: list[TurnEvent] = []
    for raw_line in raw.splitlines()[-max_lines:]:
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        role = entry.get("role") or entry.get("type", "")
        message = entry.get("message", {})
        if not isinstance(message, dict):
            continue
        content = message.get("content")

        if isinstance(content, str):
            kind = _role_kind(role)
            text = _clean_text(content, kind)
            if text.strip():
                events.append(TurnEvent(kind=kind, text=text))
            continue

        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                kind = _role_kind(role)
                text = _clean_text(block.get("text", ""), kind)
                if text.strip():
                    events.append(TurnEvent(kind=kind, text=text))
            elif btype == "thinking":
                text = block.get("text") or block.get("thinking", "")
                if text.strip():
                    events.append(TurnEvent(kind="thinking", text=text))
            elif btype == "tool_use":
                events.append(
                    TurnEvent(
                        kind="tool_use",
                        tool_name=str(block.get("name", "tool")),
                        tool_input=block.get("input")
                        if isinstance(block.get("input"), dict)
                        else {"value": block.get("input")},
                        tool_use_id=str(block.get("id", "")),
                    )
                )
            elif btype == "tool_result":
                text, _ = _stringify_tool_result(block.get("content", ""))
                events.append(
                    TurnEvent(
                        kind="tool_result",
                        text=text,
                        tool_use_id=str(block.get("tool_use_id", "")),
                        is_error=bool(block.get("is_error", False)),
                    )
                )
    # Cap to the newest N so the share JSON stays bounded; the view
    # paginates within whatever is stored.
    return events[-_MAX_STORED_EVENTS:]


def _role_kind(role: str) -> str:
    """Map a transcript role to a renderer kind."""
    return "user" if role == "user" else "assistant"


def _clean_text(text: str, kind: str) -> str:
    """Strip Telegram-only markers (TL;DR + progress notes) from assistant text."""
    if kind != "assistant":
        return text
    # Lazy: tldr is a pure-stdlib layer module.
    from .tldr import strip_progress, strip_tldr

    return strip_progress(strip_tldr(text))


def events_to_dicts(events: list[TurnEvent]) -> list[dict]:
    """Serialize events to plain dicts for JSON storage in the share record."""
    out: list[dict] = []
    for ev in events:
        d: dict = {"kind": ev.kind}
        if ev.text:
            d["text"] = ev.text
        if ev.tool_name:
            d["tool_name"] = ev.tool_name
        if ev.tool_input is not None:
            d["tool_input"] = ev.tool_input
        if ev.tool_use_id:
            d["tool_use_id"] = ev.tool_use_id
        if ev.is_error:
            d["is_error"] = True
        out.append(d)
    return out


def events_from_dicts(items: list) -> list[TurnEvent]:
    """Rebuild events from the JSON-stored dicts (for the view route)."""
    events: list[TurnEvent] = []
    for d in items:
        if not isinstance(d, dict):
            continue
        events.append(
            TurnEvent(
                kind=str(d.get("kind", "assistant")),
                text=str(d.get("text", "")),
                tool_name=str(d.get("tool_name", "")),
                tool_input=d.get("tool_input")
                if isinstance(d.get("tool_input"), dict)
                else None,
                tool_use_id=str(d.get("tool_use_id", "")),
                is_error=bool(d.get("is_error", False)),
            )
        )
    return events

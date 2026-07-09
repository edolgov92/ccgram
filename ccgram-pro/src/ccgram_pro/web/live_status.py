"""Derive a session's live status from its terminal pane — pure + testable.

The live view needs a quick, honest read of what the agent is doing right now:
actively generating, waiting on the user, or idle. It also flags the case that
bit us repeatedly — idle while a full-screen modal (e.g. ``/usage``) is open, so
the agent looks "running" (a stale timer) but is actually stuck. The terminal
tail shown alongside is the ground truth; this is the at-a-glance summary.

Everything here is a pure function of the captured pane text, so it unit-tests
without tmux.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# How many trailing non-blank lines to inspect. The status footer + spinner +
# any prompt all live within the last handful of lines.
_TAIL_SCAN = 18

# A real numbered picker needs at least this many options (avoids treating a
# stray "1. …" line in prose as an interactive prompt).
_MIN_OPTIONS = 2

# Claude Code prints this in its footer only while a turn is generating.
_WORKING_MARK = "esc to interrupt"

# Spinner status line, e.g. "✻ Crafting… (2m 43s · ↓ 21.9k tokens · thinking
# with xhigh effort)" — matched by the elapsed timer in parens.
_TIMER_RE = re.compile(r"\(\s*(?:\d+m\s*\d+s|\d+m|\d+s)\b[^)]*\)")

# Numbered option prompt, e.g. "❯ 1. Yes, switch" / "2. No, go back".
_OPTION_RE = re.compile(r"^\s*(?:❯\s*)?\d+\.\s+\S")

# Free-text interactive prompts (permission / plan / confirm dialogs).
_AWAIT_PHRASES = (
    "do you want to proceed",
    "would you like to proceed",
    "awaiting your answer",
    "do you want to make this edit",
    "do you want to create",
    "press enter to continue",
)

# Full-screen modals that leave the agent idle-but-blocked. Each tuple is a set
# of substrings that must ALL appear. The feedback rating prompt is intentionally
# excluded — it's dismissed by simply typing, so it isn't really blocking.
_OVERLAY_SIGNATURES: tuple[tuple[str, ...], ...] = (
    ("settings", "status", "config", "usage", "stats"),  # /usage screen tabs
    ("current session", "% used"),  # /usage body
    ("switch model?",),
    ("set model to",),
)


@dataclass(frozen=True)
class LiveStatus:
    """At-a-glance session state for the live banner."""

    state: str  # "working" | "awaiting" | "idle"
    label: str
    detail: str  # spinner line / prompt gist; may be empty
    overlay: bool  # a full-screen modal appears to be open

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "label": self.label,
            "detail": self.detail,
            "overlay": self.overlay,
        }


def _tail(pane_text: str) -> list[str]:
    lines = [ln.rstrip() for ln in (pane_text or "").splitlines() if ln.strip()]
    return lines[-_TAIL_SCAN:]


def terminal_tail(pane_text: str, n: int) -> list[str]:
    """Last *n* non-blank pane lines — the ground-truth panel for the live view."""
    lines = [ln.rstrip() for ln in (pane_text or "").splitlines() if ln.strip()]
    return lines[-n:]


def _clean_spinner(line: str) -> str:
    # Strip the leading spinner glyph / bullet so the banner reads cleanly.
    return line.lstrip("✻✶✽✢✳●*·⁙ ").strip()


def _spinner_detail(tail: list[str]) -> str:
    for line in reversed(tail):
        if _TIMER_RE.search(line):
            return _clean_spinner(line)
    return ""


def _is_awaiting(tail: list[str], lower: str) -> bool:
    if any(phrase in lower for phrase in _AWAIT_PHRASES):
        return True
    # A numbered-option prompt needs at least two options to be a real picker.
    return sum(1 for line in tail if _OPTION_RE.match(line)) >= _MIN_OPTIONS


def _await_detail(tail: list[str]) -> str:
    for line in tail:
        if _OPTION_RE.match(line):
            return line.strip()
    return ""


def _detect_overlay(lower: str) -> bool:
    return any(all(tok in lower for tok in sig) for sig in _OVERLAY_SIGNATURES)


def derive_status(pane_text: str) -> LiveStatus:
    """Classify the session from its captured pane text."""
    tail = _tail(pane_text)
    lower = "\n".join(tail).lower()
    overlay = _detect_overlay(lower)

    if _WORKING_MARK in lower:
        return LiveStatus("working", "Working", _spinner_detail(tail), overlay=False)

    if _is_awaiting(tail, lower):
        return LiveStatus(
            "awaiting", "Awaiting your answer", _await_detail(tail), overlay=overlay
        )

    label = "Idle — a screen is open" if overlay else "Idle"
    return LiveStatus("idle", label, "", overlay=overlay)

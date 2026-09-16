"""Auto-answer Claude Code's own blocking TUI dialogs so a session never stalls.

Claude Code interrupts a session with modal prompts that are about the TOOL,
not about the user's task — a stale-session resume offer, a drafted bug report,
a feedback opt-out. They block the input line, so through Telegram they surface
as ccgram's raw arrow-key toolbar at best; at worst the next forwarded message
is typed into the modal and silently swallowed (seen live: a 32-line message
lost, the progress bubble spinning for 11 hours while nothing ran).

Each dialog here has one obviously-right answer that keeps working and never
acts outward on the user's behalf:

- **Resume from summary?** → *Resume full session as-is*. A summary loses the
  working context the session exists for.
- **Bug report drafted (1 review · 2 send · 0 dismiss)** → *dismiss*. Sending
  feedback to Anthropic is the user's call, never ours; dismissing only
  unblocks the session.
- **Turn off Claude-drafted feedback? (0 off · Esc keep)** → *Esc, keep*. This
  is a settings change; leave the user's configuration exactly as it was.

Recognition requires EVERY marker of a dialog to be on the captured pane, so a
fragment left in scrollback can never trigger an answer. Wired into
:mod:`interactive_state`'s ``handle_interactive_ui`` guard, so both detection
paths (the 1s poll tick and the Notification hook) hit it before the scraped UI
is posted; a short per-window cooldown stops the two from double-driving.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

# Zero-based index of "Resume full session as-is" in the resume dialog.
FULL_SESSION_INDEX = 1

_COOLDOWN_SECONDS = 5.0
_last_answered: dict[str, float] = {}


@dataclass(frozen=True)
class Dialog:
    """One recognised Claude Code modal and the answer we give it.

    ``markers`` must ALL appear on the pane. ``keys`` is the literal tmux key
    sequence to send; when empty, ``select_index`` drives a numbered selector
    instead (reset-to-top → down → enter).
    """

    name: str
    markers: tuple[str, ...]
    keys: tuple[str, ...] = ()
    select_index: int | None = None


DIALOGS: tuple[Dialog, ...] = (
    Dialog(
        name="resume_summary",
        markers=("Resume from summary", "Resume full session as-is"),
        select_index=FULL_SESSION_INDEX,
    ),
    Dialog(
        name="bug_report",
        # "0" dismisses the draft. We never press "2" (send to Anthropic).
        markers=("Bug report drafted", "to dismiss"),
        keys=("0",),
    ),
    Dialog(
        name="feedback_optout",
        # Esc keeps the current setting — turning the feature off is the
        # user's decision, not ours.
        markers=("Turn off Claude-drafted feedback?", "Esc to keep"),
        keys=("Escape",),
    ),
)


def match_dialog(pane_text: str) -> Dialog | None:
    """Return the recognised dialog on *pane_text*, or None."""
    for dialog in DIALOGS:
        if all(marker in pane_text for marker in dialog.markers):
            return dialog
    return None


def is_blocking_dialog(pane_text: str) -> bool:
    """True when the pane shows a Claude Code modal we know how to answer."""
    return match_dialog(pane_text) is not None


async def auto_answer(window_id: str) -> bool:
    """Answer a recognised Claude Code modal on *window_id*, if one is up.

    Returns True when a dialog was detected and answered (callers then skip
    posting any interactive UI for it). Never raises — a failure here must not
    lose the prompt, so the scraped UI stays as the fallback.
    """
    # Lazy: tmux_manager is the live tmux session wrapper.
    from ccgram.tmux_manager import tmux_manager

    # Lazy: sibling module; deferred to keep import edges minimal.
    from . import interactive_drive

    try:
        pane_text = await tmux_manager.capture_pane(window_id)
    except OSError, ValueError:
        return False
    if not pane_text:
        return False
    dialog = match_dialog(pane_text)
    if dialog is None:
        return False

    now = time.monotonic()
    if now - _last_answered.get(window_id, 0.0) < _COOLDOWN_SECONDS:
        return True  # already driven a moment ago — let the TUI catch up
    _last_answered[window_id] = now

    if dialog.select_index is not None:
        ok = await interactive_drive.drive_single_select(window_id, dialog.select_index)
    else:
        ok = await interactive_drive.press_keys(window_id, list(dialog.keys))
    if ok:
        logger.info("tui_dialog_auto_answered", window_id=window_id, dialog=dialog.name)
    else:
        logger.warning(
            "tui_dialog_auto_answer_failed", window_id=window_id, dialog=dialog.name
        )
    return ok


def _reset_for_testing() -> None:
    _last_answered.clear()

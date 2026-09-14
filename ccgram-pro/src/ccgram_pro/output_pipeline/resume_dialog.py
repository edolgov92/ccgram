"""Auto-answer Claude Code's "stale session" resume dialog — always full session.

When a long-idle, large session is picked up again (or resumed), Claude Code
shows a blocking TUI dialog:

    This session is 1d 2h old and 316.3k tokens.
    Resuming the full session will consume a substantial portion of your
    usage limits. We recommend resuming from a summary.
      1. Resume from summary (recommended)
      2. Resume full session as-is
      3. Don't ask me again            (newer builds only)

Through Telegram that surfaces as ccgram's raw arrow-key toolbar — noise the
user has to drive by hand every time. The standing decision is "always the
full session" (a summary loses the working context the session exists for),
so this module recognises the dialog on the captured pane and picks option 2
deterministically via :func:`interactive_drive.drive_single_select`.

Wired into :mod:`interactive_state`'s ``handle_interactive_ui`` guard, so both
detection paths (1s poll tick and the Notification hook) hit it before the
scraped UI is posted. A short per-window cooldown prevents double-driving when
both paths fire within the same second.
"""

from __future__ import annotations

import time

import structlog

logger = structlog.get_logger()

# Zero-based index of "Resume full session as-is" in the dialog's option list.
FULL_SESSION_INDEX = 1

# Both markers must be present — the title alone also appears in unrelated
# usage hints, and the option label alone could sit in scrollback history.
_DIALOG_MARKERS = ("Resume from summary", "Resume full session as-is")

_COOLDOWN_SECONDS = 5.0
_last_answered: dict[str, float] = {}


def is_stale_resume_dialog(pane_text: str) -> bool:
    """True when the captured pane shows the resume-from-summary dialog."""
    return all(marker in pane_text for marker in _DIALOG_MARKERS)


async def auto_answer(window_id: str) -> bool:
    """Pick "Resume full session as-is" if the dialog is on screen.

    Returns True when the dialog was detected and answered (callers then skip
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
    if not pane_text or not is_stale_resume_dialog(pane_text):
        return False

    now = time.monotonic()
    if now - _last_answered.get(window_id, 0.0) < _COOLDOWN_SECONDS:
        return True  # already driven a moment ago — let the TUI catch up
    _last_answered[window_id] = now

    ok = await interactive_drive.drive_single_select(window_id, FULL_SESSION_INDEX)
    if ok:
        logger.info("resume_dialog_auto_answered", window_id=window_id, choice="full")
    else:
        logger.warning("resume_dialog_auto_answer_failed", window_id=window_id)
    return ok


def _reset_for_testing() -> None:
    _last_answered.clear()

"""Drive a Claude pane into a target permission mode via Shift+Tab.

New sessions enter plan mode deterministically through the
``--permission-mode plan`` launch flag (see ``new_session``), so there is no
auto-entry orchestration anymore. This module exists for *mid-session* mode
changes from the Settings panel, where the only lever is the TUI's Shift+Tab
cycle (default → acceptEdits → plan → [bypassPermissions] → [auto] → default).

:func:`drive_to_mode` presses Shift+Tab in a bounded loop, re-scraping the
status line after each press until the desired mode is reached, so it works
from any starting point in the cycle.
"""

from __future__ import annotations

import asyncio

import structlog

logger = structlog.get_logger()

# The cycle has at most ~5 stops; 6 presses guarantees a full revolution so we
# never loop forever if scraping is flaky.
_MAX_PRESSES = 6
_PRESS_DELAY_SECONDS = 0.5
_PLAN_LABELS = frozenset({"Plan", "plan"})


def _matches(label: str | None, target: str) -> bool:
    if label is None:
        return False
    if target == "plan":
        return label in _PLAN_LABELS
    # "coding" = any non-plan mode that the scraper recognises.
    return label not in _PLAN_LABELS


async def _scrape(window_id: str) -> str | None:
    # Lazy: pulls the provider registry which boots all known providers.
    from ccgram.providers.claude import ClaudeProvider

    try:
        return await ClaudeProvider().scrape_current_mode(window_id)
    except Exception:  # noqa: BLE001 -- scrape is best-effort
        return None


async def drive_to_mode(window_id: str, target: str) -> bool:
    """Drive the pane to *target* ("plan" or "coding"). Returns success.

    Returns True as soon as the scraped label matches (including when it
    already matched, sending zero keystrokes). Returns False if the target is
    not reached within the bounded number of presses or tmux send fails.
    """
    # Lazy: ccgram internal — deferred to avoid an import cycle with bootstrap.
    from ccgram.tmux_manager import tmux_manager

    label = await _scrape(window_id)
    if _matches(label, target):
        return True
    for _ in range(_MAX_PRESSES):
        try:
            await tmux_manager.send_keys(window_id, "BTab", literal=False, enter=False)
        except Exception:  # noqa: BLE001 -- tmux flake is non-fatal
            logger.warning("mode-drive BTab failed for %s", window_id, exc_info=True)
            return False
        await asyncio.sleep(_PRESS_DELAY_SECONDS)
        label = await _scrape(window_id)
        if _matches(label, target):
            return True
    logger.debug("mode-drive: %s did not reach %s within bound", window_id, target)
    return False

"""Deterministically drive Claude Code's interactive TUI selectors via tmux keys.

Claude's selector has no number-key shortcuts and we get no cursor-position
feedback, so we make selection deterministic by always resetting to a known
origin first: send ``Up`` many times (the list clamps at the top), then move
``Down`` exactly *index* times and press ``Enter``. Multi-select walks down
toggling ``Space`` at each chosen row (cursor tracked relative to the reset
origin), then ``Enter``. Cancel is ``Escape``.

All keys go through ``tmux_manager.send_keys(..., enter=False, literal=False)``
so ``Up``/``Down``/``Enter``/``Space``/``Escape`` are interpreted as special
keys (matching ccgram's own ``interactive_callbacks``).
"""

from __future__ import annotations

import asyncio

import structlog

logger = structlog.get_logger()

# Generous reset: more than any realistic option count so a top-clamping list
# always lands on the first row regardless of where the cursor started.
_RESET_PRESSES = 12
# Small gap between keystrokes so the TUI processes each before the next.
_KEY_DELAY = 0.05


async def _resolve_target(window_id: str) -> str | None:
    # Lazy: tmux_manager is the live tmux session wrapper.
    from ccgram.tmux_manager import tmux_manager

    window = await tmux_manager.find_window_by_id(window_id)
    return window.window_id if window else None


async def _press(target_window_id: str, key: str, count: int = 1) -> bool:
    # Lazy: tmux_manager is the live tmux session wrapper.
    from ccgram.tmux_manager import tmux_manager

    for _ in range(count):
        ok = await tmux_manager.send_keys(
            target_window_id, key, enter=False, literal=False
        )
        if not ok:
            return False
        await asyncio.sleep(_KEY_DELAY)
    return True


async def drive_single_select(window_id: str, index: int) -> bool:
    """Select option *index* in a single-select selector (reset → down → enter)."""
    target = await _resolve_target(window_id)
    if target is None:
        logger.debug("drive_single_select: window %s gone", window_id)
        return False
    if not await _press(target, "Up", _RESET_PRESSES):
        return False
    if index > 0 and not await _press(target, "Down", index):
        return False
    return await _press(target, "Enter")


async def drive_multi_select(window_id: str, indices: list[int]) -> bool:
    """Toggle each chosen option then confirm (reset → [down*, space]… → enter)."""
    if not indices:
        return False
    target = await _resolve_target(window_id)
    if target is None:
        return False
    if not await _press(target, "Up", _RESET_PRESSES):
        return False
    cursor = 0
    for option in sorted(set(indices)):
        steps = option - cursor
        if steps > 0 and not await _press(target, "Down", steps):
            return False
        cursor = option
        if not await _press(target, "Space"):
            return False
    return await _press(target, "Enter")


async def drive_cancel(window_id: str) -> bool:
    """Dismiss the prompt with Escape."""
    target = await _resolve_target(window_id)
    if target is None:
        return False
    return await _press(target, "Escape")

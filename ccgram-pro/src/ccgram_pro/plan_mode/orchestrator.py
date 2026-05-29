"""Schedule and execute plan-mode entry for newly-created Claude windows.

The orchestrator watches for completion of :func:`_create_window_and_bind`
and — for Claude windows with ``settings.defaults.plan_mode_on_new_session``
enabled — kicks off a per-window asyncio task that:

1. Polls ``providers.claude.scrape_current_mode`` every ``_POLL_INTERVAL``
   seconds, up to ``_MAX_READY_WAIT`` seconds, until the pane shows a
   recognised mode label (i.e. Claude's prompt is rendered).
2. Sends the Shift+Tab key sequence via tmux to toggle plan mode.
3. Re-polls to confirm the mode is now "Plan"; retries once on the path
   that fired but didn't flip (e.g. Shift+Tab arrived mid-render).
4. Records the final state on the sidecar so other components can see
   whether plan mode landed.

Failures are logged but never raise — plan-mode entry is best-effort UX.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from ..config import load_settings
from .. import state

logger = structlog.get_logger()

_POLL_INTERVAL_SECONDS = 0.4
_MAX_READY_WAIT_SECONDS = 12.0
_MODE_VERIFY_DELAY_SECONDS = 0.6
_PLAN_LABELS = frozenset({"Plan", "plan"})

# Guard for idempotent install on hot-reload / test harness.
_installed = False


async def _wait_for_prompt_ready(window_id: str) -> str | None:
    """Return the mode label once the Claude pane is responsive, else None."""
    # Lazy: pulls the provider registry which boots all known providers.
    from ccgram.providers.claude import ClaudeProvider

    provider = ClaudeProvider()
    deadline = asyncio.get_running_loop().time() + _MAX_READY_WAIT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        try:
            label = await provider.scrape_current_mode(window_id)
        except Exception:  # noqa: BLE001 -- scrape is best-effort
            label = None
        if label:
            return label
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    return None


async def _send_shift_tab(window_id: str) -> bool:
    """Send the Shift+Tab key sequence to the window. Returns success."""
    # Lazy: ccgram internal — deferred to avoid an import cycle with ccgram bootstrap (the layer is imported during bootstrap).
    from ccgram.tmux_manager import tmux_manager

    try:
        await tmux_manager.send_keys(window_id, "BTab", literal=False, enter=False)
    except Exception:  # noqa: BLE001 -- tmux flake should be loud but non-fatal
        logger.warning("plan-mode BTab failed for %s", window_id, exc_info=True)
        return False
    return True


async def _verify_plan_mode(window_id: str) -> bool:
    """Re-poll the mode label; return True when it says plan."""
    # Lazy: ccgram internal — deferred to avoid an import cycle with ccgram bootstrap (the layer is imported during bootstrap).
    from ccgram.providers.claude import ClaudeProvider

    provider = ClaudeProvider()
    await asyncio.sleep(_MODE_VERIFY_DELAY_SECONDS)
    try:
        label = await provider.scrape_current_mode(window_id)
    except Exception:  # noqa: BLE001 -- best-effort verification
        return False
    return bool(label and label in _PLAN_LABELS)


async def _record_state(window_id: str, *, status: str) -> None:
    """Persist the outcome on the sidecar so Phase 4 callers can branch on it."""
    async with state.transaction(window_id):
        sidecar = state.get_or_create(window_id)
        sidecar.plan_mode = status
        state.save(sidecar)


async def _enter_plan_mode(window_id: str) -> None:
    """The full sequence; runs as a per-window background task."""
    logger.info("plan-mode entry scheduled for %s", window_id)
    ready_label = await _wait_for_prompt_ready(window_id)
    if ready_label is None:
        logger.info("plan-mode entry timed out waiting for readiness on %s", window_id)
        await _record_state(window_id, status="skipped")
        return
    if ready_label in _PLAN_LABELS:
        logger.info("plan-mode entry: %s already in plan mode", window_id)
        await _record_state(window_id, status="entered")
        return

    if not await _send_shift_tab(window_id):
        await _record_state(window_id, status="skipped")
        return

    if await _verify_plan_mode(window_id):
        logger.info("plan-mode entry: %s flipped to plan", window_id)
        await _record_state(window_id, status="entered")
        return

    # One retry — Shift+Tab can race with Claude's first render.
    logger.debug("plan-mode entry: first BTab didn't flip %s, retrying", window_id)
    if not await _send_shift_tab(window_id):
        await _record_state(window_id, status="skipped")
        return
    if await _verify_plan_mode(window_id):
        logger.info("plan-mode entry: %s flipped to plan after retry", window_id)
        await _record_state(window_id, status="entered")
        return
    logger.warning(
        "plan-mode entry: %s remained off plan after retry; giving up", window_id
    )
    await _record_state(window_id, status="skipped")


def _should_enter_plan(provider_name: str) -> bool:
    """Per-settings gate. Only claude has a plan-mode toggle we know how to hit."""
    if provider_name != "claude":
        return False
    return load_settings().defaults.plan_mode_on_new_session


def install_plan_mode_entry() -> None:
    """Wrap ``_create_window_and_bind`` to schedule plan-mode entry tasks.

    The wrapper inspects the resolved provider_name argument; for claude
    windows when the setting is on, it spawns the entry task *after*
    the original returns so the binding is already in place.
    """
    global _installed
    if _installed:
        return

    # Lazy: the upstream callbacks module pulls in PTB types.
    from ccgram.handlers.topics import directory_callbacks as dc_mod

    original = dc_mod._create_window_and_bind

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        # _create_window_and_bind signature is positional:
        # (query, user_id, selected_path, provider_name, approval_mode,
        # context) → provider_name is the 4th positional (index 3).
        provider_name_pos = 4
        provider_name = ""
        if len(args) >= provider_name_pos:
            provider_name = str(args[3])
        elif "provider_name" in kwargs:
            provider_name = str(kwargs["provider_name"])
        if not _should_enter_plan(provider_name):
            return result
        # The most recently-created window id is the one we want. Walk
        # tmux's window state — ccgram's bookkeeping has not necessarily
        # propagated yet at this point.
        # Lazy: ccgram internal — deferred to avoid an import cycle with ccgram bootstrap (the layer is imported during bootstrap).
        from ccgram.tmux_manager import tmux_manager

        try:
            windows = await tmux_manager.list_windows()
        except Exception:  # noqa: BLE001 -- never fail bind because of a list call
            logger.debug("could not list windows for plan-mode hand-off")
            return result
        # ``list_windows`` returns a list of LibTmuxWindow-likes ordered
        # by tmux index; the newest is last.
        if not windows:
            return result
        target_window_id = windows[-1].window_id
        task = asyncio.create_task(_enter_plan_mode(target_window_id))
        # Detach the task — orchestrator owns its own lifecycle.
        task.add_done_callback(lambda _t: None)
        return result

    wrapped.__name__ = "_create_window_and_bind_with_plan_mode"
    wrapped.__qualname__ = wrapped.__name__
    dc_mod._create_window_and_bind = wrapped  # type: ignore[assignment]
    _installed = True
    logger.info(
        "ccgram-pro plan-mode entry installed — new Claude sessions auto-enter plan mode"
    )


def _reset_for_testing() -> None:
    """Drop the install guard."""
    global _installed
    _installed = False

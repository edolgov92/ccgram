"""Shared ownership flag + guard so the clean UI suppresses ccgram's scraped one.

When the layer posts its clean AskUserQuestion / plan keyboard for a topic, it
must stop ccgram from ALSO posting its screen-scraped arrow-key keyboard for the
same prompt. ccgram triggers that scraped UI from three places —
``message_routing.handle_new_message`` (fast path), the 1s polling tick
(``window_tick.apply``), and the ``Notification`` hook fallback — all via
``handle_interactive_ui``.

So we (a) mark a topic "owned" the instant a clean prompt is being set up (before
the transcript read, to win the race against the fast path), and (b) wrap
``handle_interactive_ui`` at every importer to no-op (report handled) while the
topic is owned. Ownership is released when the user answers (``_clear``) or when
the clean handler fails and falls back to the scraped UI.

This module holds ONLY the ownership set + the guard install, so both
``interactive_clean`` and ``interactive_plan`` can use it without an import cycle.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

# (user_id, thread_id) topics whose interactive prompt is owned by the clean UI.
_owned: set[tuple[int, int]] = set()
_guard_installed = False


def claim(user_id: int, thread_id: int) -> None:
    _owned.add((user_id, thread_id))


def release(user_id: int, thread_id: int) -> None:
    _owned.discard((user_id, thread_id))


def is_owned(user_id: int, thread_id: int) -> bool:
    return (user_id, thread_id) in _owned


def _wrap_handle_interactive_ui(original: Any) -> Any:
    async def wrapped(
        client: Any,
        user_id: int,
        window_id: str,
        thread_id: int | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        if thread_id is not None and is_owned(user_id, thread_id):
            # The clean keyboard owns this prompt — report "handled" so callers
            # don't post the scraped UI or clear interactive mode.
            return True
        return await original(client, user_id, window_id, thread_id, *args, **kwargs)

    wrapped.__wrapped__ = original  # type: ignore[attr-defined]
    wrapped.__name__ = "ccgrampro_guarded_handle_interactive_ui"
    return wrapped


def install_interactive_guard() -> None:
    """Patch ``handle_interactive_ui`` at every importer to honor ownership."""
    global _guard_installed
    if _guard_installed:
        return
    try:
        mods = _interactive_ui_importers()
    except ImportError, AttributeError:
        logger.warning("interactive guard: import failed; scraped UI not suppressed")
        return
    ui_mod = mods[0]
    if getattr(ui_mod.handle_interactive_ui, "__wrapped__", None) is not None:
        _guard_installed = True
        return
    wrapped = _wrap_handle_interactive_ui(ui_mod.handle_interactive_ui)
    for mod in mods:
        if hasattr(mod, "handle_interactive_ui"):
            mod.handle_interactive_ui = wrapped  # type: ignore[attr-defined]
    _guard_installed = True


def _interactive_ui_importers() -> list[Any]:
    """Return [canonical, *importers] modules that bind ``handle_interactive_ui``."""
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers import hook_events as hook_events_mod

    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.interactive import interactive_callbacks as cb_mod

    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.interactive import interactive_ui as ui_mod

    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.messaging_pipeline import message_routing as routing_mod

    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.polling.window_tick import apply as apply_mod

    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.handlers.text import text_handler as text_mod

    return [ui_mod, hook_events_mod, routing_mod, apply_mod, text_mod, cb_mod]


def _reset_for_testing() -> None:
    global _guard_installed
    _owned.clear()
    _guard_installed = False

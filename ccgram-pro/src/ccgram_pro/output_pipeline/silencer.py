"""Silencer — patches ccgram's chatty UX so silent-mode topics stay quiet.

Targets four upstream behaviours that fire on every polling tick or Claude
event and would otherwise blow up the chat with edits, renames, and
typing indicators:

- ``handlers.status.topic_emoji.update_topic_emoji`` (also imported into
  ``polling.window_tick.apply``) → renames the topic with a status emoji.
- ``handlers.messaging_pipeline.message_queue.enqueue_status_update``
  (re-imported into ``polling.window_tick.apply``) → the status bubble
  message with ``[Esc][📸][Last][Get File]`` inline keyboard.
- ``polling.window_tick.apply._send_typing_throttled`` → ``typing…``
  chat-action that pings every poll cycle while the topic is "active".

Wrapping happens once at extension install time. The originals stay
reachable through the wrappers so a window with ``silent_mode = False``
gets the full dashboard.

Threading model: the wrappers run on the polling-loop task; sidecar reads
are synchronous file I/O so they do not yield the loop. Concurrent
patches of the same module attribute would race, but ccgram-pro is the
only extension touching these symbols today; if that changes we'll move
to a registry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from .. import state
from ..config import load_settings

if TYPE_CHECKING:
    from collections.abc import Callable

logger = structlog.get_logger()

# Module-level flag so the install is idempotent — extension.install may
# run twice in the test harness, and double-wrapping would lose the
# original references.
_installed = False


def _resolve_window_for_thread(thread_id: int | None) -> str | None:
    """Map ``thread_id`` → ``window_id`` via ccgram's thread router.

    Returns ``None`` when the thread is unbound (e.g. a topic still in
    its setup flow) — in that case the silencer falls through to the
    original behaviour, since the noisy events are usually part of the
    first-time setup the user does want to see.

    Iterates ``thread_router.thread_bindings`` (public dict published by
    ccgram's ``ThreadRouter`` — see ``src/ccgram/thread_router.py``)
    rather than calling ``get_window_for_thread`` per user because we do
    not have the user_id at the call site.
    """
    if thread_id is None:
        return None
    # Lazy: thread_router is wired by ccgram bootstrap; importing eagerly
    # would couple the silencer to bootstrap ordering.
    from ccgram.thread_router import thread_router

    for thread_map in thread_router.thread_bindings.values():
        window_id = thread_map.get(thread_id)
        if window_id:
            return window_id
    return None


def _is_silent_for_thread(thread_id: int | None) -> bool:
    """Return True when the window bound to *thread_id* opted into silent mode.

    The default is **on** — newly created sidecars have ``silent_mode =
    True``. A window without a sidecar yet (first-time topic flow) is
    treated as non-silent so the user still sees the directory picker.
    """
    window_id = _resolve_window_for_thread(thread_id)
    if window_id is None:
        return False
    sidecar = state.load(window_id)
    if sidecar is None:
        # No sidecar yet — fall back to the global default from
        # settings.toml so the operator can tune the first-message
        # experience without editing per-window files.
        return load_settings().defaults.silent_mode
    return sidecar.silent_mode


def _is_silent_for_chat_thread(chat_id: int, thread_id: int) -> bool:
    """Resolution variant for callsites that have ``(chat_id, thread_id)``."""
    del chat_id  # chat_id is the group; binding lookup is per-thread
    return _is_silent_for_thread(thread_id)


def _is_silent_for_window(window_id: str | None) -> bool:
    """Resolution variant for callsites that have ``window_id`` directly.

    Preferred over the thread-id variant when available — the window_id
    is always non-None at the polling-loop emit sites, whereas
    ``thread_id`` is an optional kwarg that some callers omit. Defaults
    to the global ``settings.defaults.silent_mode`` for windows without
    a sidecar (newly-created topics).
    """
    if not window_id:
        return False
    sidecar = state.load(window_id)
    if sidecar is None:
        return load_settings().defaults.silent_mode
    return sidecar.silent_mode


def _wrap_async(
    name: str,
    original: Callable[..., Any],
    skip_when_silent: Callable[..., bool],
) -> Callable[..., Any]:
    """Return an async wrapper that no-ops when ``skip_when_silent`` returns True."""

    async def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        try:
            if skip_when_silent(*args, **kwargs):
                logger.debug("silencer: skipped %s for silent topic", name)
                return None
        except Exception:  # noqa: BLE001 -- silencer must never abort the host
            logger.exception("silencer guard for %s raised; falling through", name)
        return await original(*args, **kwargs)

    wrapper.__name__ = f"silenced_{name}"
    wrapper.__qualname__ = wrapper.__name__
    wrapper.__wrapped__ = original  # type: ignore[attr-defined]
    return wrapper


# ── Predicates for each patched function ───────────────────────────────


def _topic_emoji_silent(
    _client: object,
    _chat_id: int,
    thread_id: int,
    _state: str,
    _display_name: str,
    *_args: Any,
    **_kwargs: Any,
) -> bool:
    return _is_silent_for_thread(thread_id)


def _status_update_silent(
    _client: object,
    _user_id: int,
    window_id: str,
    _text: object,
    *_args: Any,
    **_kwargs: Any,
) -> bool:
    # window_id is always present (3rd positional); thread_id is an
    # optional kwarg some call sites omit, so checking by window_id is
    # more reliable.
    return _is_silent_for_window(window_id)


def _typing_silent(
    _bot: object,
    _user_id: int,
    thread_id: int | None,
    *_args: Any,
    **_kwargs: Any,
) -> bool:
    # Typing-throttle in silent mode: never send. The ack reaction (emoji
    # added to the user's own message) is the "read" indicator instead;
    # an ever-present "typing…" badge confused the user post-completion
    # and is one of the bugs this silencer was built to fix.
    return _is_silent_for_thread(thread_id)


def _is_silent_for_session(session_id: str) -> bool:
    """Resolve session_id → window_id → sidecar.silent_mode."""
    # Lazy: session_query indirectly pulls in session_manager and the
    # entire query layer; deferring the import keeps cold-test imports
    # cheap.
    from ccgram import session_query

    for _user_id, window_id, _thread_id in session_query.find_users_for_session(
        session_id
    ):
        sidecar = state.load(window_id)
        if sidecar is None:
            return load_settings().defaults.silent_mode
        return sidecar.silent_mode
    return False


def _handle_new_message_silent(msg: object, *_args: Any, **_kwargs: Any) -> bool:
    """Suppress noisy transcript messages for silent-mode windows.

    ccgram routes every transcript entry through ``handle_new_message`` —
    including the user's own message (which Claude records in the
    transcript) AND Claude's assistant text turn. For silent-mode topics
    we drop everything; the Stop summarizer is responsible for posting
    the single user-facing message that wraps Claude's response (short
    response → posted verbatim; long response → LLM-summarised).

    Allow-through cases:

    - Interactive prompts (ExitPlanMode / AskUserQuestion / Permission) are
      blocking UI, not chatter — they MUST reach the user even in silent mode.
      Letting ``handle_new_message`` run fires ccgram's interactive detection
      immediately (the fast path), instead of waiting on the Notification hook
      or the 1s polling tick.
    """
    session_id = getattr(msg, "session_id", "")
    if not session_id:
        return False
    if getattr(msg, "content_type", "") == "tool_use" and _is_interactive_tool(
        getattr(msg, "tool_name", "") or ""
    ):
        return False
    # Suppress everything else ccgram would route through here in silent mode;
    # the Stop summarizer is what the user sees instead.
    return _is_silent_for_session(session_id)


def _is_interactive_tool(tool_name: str) -> bool:
    if not tool_name:
        return False
    # Lazy: interactive package pulls PTB types; only needed on this check.
    from ccgram.handlers.interactive import INTERACTIVE_TOOL_NAMES

    return tool_name in INTERACTIVE_TOOL_NAMES


# ── Install / uninstall ────────────────────────────────────────────────


def install_silencer() -> None:
    """Patch ccgram's chatty surfaces. Idempotent + reversible via uninstall.

    The hard lesson learned the painful way: when a module does
    ``from foo import bar``, the name ``bar`` is bound to the imported
    module's globals AT IMPORT TIME, *not* by lookup at call time.
    Patching ``foo.bar = wrapped`` afterwards has no effect on the
    importer — it already captured the original reference. So this
    install walks every known importer of each function and replaces
    the bound reference in-place.
    """
    global _installed
    if _installed:
        return

    # Each group is fault-isolated: an upstream rename that breaks one patch
    # (ImportError/AttributeError) degrades that single feature, not the whole
    # silencer. Already-wrapped attributes (re-install after a partial failure)
    # are detected via ``__wrapped__`` so we never double-wrap.
    ok = (
        _install_topic_emoji()
        & _install_status_bubble()
        & _install_typing()
        & _install_new_message_echo()
    )

    _installed = True
    logger.info(
        "ccgram-pro silencer installed%s — silent_mode topics suppress topic "
        "emoji, status bubble, typing indicator, and user/thinking echo",
        "" if ok else " (with degraded patches; see warnings)",
    )


def _already_wrapped(value: Any) -> bool:
    return getattr(value, "__wrapped__", None) is not None


def _patch_importers(
    attr: str, wrapped: Callable[..., Any], modules: list[Any]
) -> None:
    for mod in modules:
        if hasattr(mod, attr) and not _already_wrapped(getattr(mod, attr)):
            setattr(mod, attr, wrapped)


def _install_topic_emoji() -> bool:
    try:
        # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
        from ccgram.handlers import hook_events as hook_events_mod

        # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
        from ccgram.handlers.polling.window_tick import apply as apply_mod

        # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
        from ccgram.handlers.status import topic_emoji as topic_emoji_mod

        if _already_wrapped(topic_emoji_mod.update_topic_emoji):
            return True
        wrapped = _wrap_async(
            "update_topic_emoji",
            topic_emoji_mod.update_topic_emoji,
            _topic_emoji_silent,
        )
        _patch_importers(
            "update_topic_emoji", wrapped, [apply_mod, topic_emoji_mod, hook_events_mod]
        )
        return True
    except ImportError, AttributeError:
        logger.warning("silencer: topic-emoji patch skipped", exc_info=True)
        return False


def _install_status_bubble() -> bool:
    try:
        # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
        from ccgram.handlers import cleanup as cleanup_mod

        # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
        from ccgram.handlers import hook_events as hook_events_mod

        # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
        from ccgram.handlers.commands import forward as forward_mod

        # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
        from ccgram.handlers.messaging_pipeline import (
            message_queue as message_queue_mod,
        )

        # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
        from ccgram.handlers.polling.window_tick import apply as apply_mod

        # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
        from ccgram.handlers.shell import shell_commands as shell_commands_mod

        # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
        from ccgram.handlers.text import text_handler as text_handler_mod

        if _already_wrapped(message_queue_mod.enqueue_status_update):
            return True
        wrapped = _wrap_async(
            "enqueue_status_update",
            message_queue_mod.enqueue_status_update,
            _status_update_silent,
        )
        _patch_importers(
            "enqueue_status_update",
            wrapped,
            [
                message_queue_mod,
                apply_mod,
                hook_events_mod,
                cleanup_mod,
                forward_mod,
                shell_commands_mod,
                text_handler_mod,
            ],
        )
        return True
    except ImportError, AttributeError:
        logger.warning("silencer: status-bubble patch skipped", exc_info=True)
        return False


def _install_typing() -> bool:
    try:
        # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
        from ccgram.handlers.polling.window_tick import apply as apply_mod

        if _already_wrapped(apply_mod._send_typing_throttled):
            return True
        apply_mod._send_typing_throttled = _wrap_async(  # type: ignore[assignment]
            "_send_typing_throttled", apply_mod._send_typing_throttled, _typing_silent
        )
        return True
    except ImportError, AttributeError:
        logger.warning("silencer: typing patch skipped", exc_info=True)
        return False


def _install_new_message_echo() -> bool:
    # handle_new_message is imported into bootstrap to build the SessionMonitor
    # callback closure; patching bootstrap's binding is what actually takes
    # effect, since the closure resolves the name via bootstrap's globals.
    try:
        # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
        from ccgram import bootstrap as bootstrap_mod

        # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
        from ccgram.handlers.messaging_pipeline import (
            message_routing as message_routing_mod,
        )

        if _already_wrapped(message_routing_mod.handle_new_message):
            return True
        wrapped = _wrap_async(
            "handle_new_message",
            message_routing_mod.handle_new_message,
            _handle_new_message_silent,
        )
        message_routing_mod.handle_new_message = wrapped  # type: ignore[assignment]
        bootstrap_mod.handle_new_message = wrapped  # type: ignore[assignment]
        return True
    except ImportError, AttributeError:
        logger.warning("silencer: new-message-echo patch skipped", exc_info=True)
        return False


def _reset_for_testing() -> None:
    """Drop the installed flag. Tests that re-install need a clean slate."""
    global _installed
    _installed = False

"""Augment ccgram's native ExitPlanMode prompt with a ⚙️ Settings button.

Plan mode is *entered* deterministically via the ``--permission-mode plan``
launch flag (see ``new_session``). When Claude finishes planning it calls
``ExitPlanMode``, and ccgram's interactive UI already detects that prompt,
renders the arrow/Tab/Enter/Esc keyboard, and drives the pane — this works
even under ``--dangerously-skip-permissions`` because plan mode is orthogonal
to permissions, and it surfaces in silent topics via the Notification hook and
the polling tick (the silencer also whitelists interactive prompts for the
fast path).

We reuse that native picker (the user's choice) and only *augment* it: a
``[⚙️ Settings]`` row is appended for ExitPlanMode prompts so the user can
adjust model/reasoning/mode from the same message. We never replace the real
keyboard — if augmentation raises, ccgram's stock UI is used verbatim.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

_installed = False


def install_plan_approval_surface() -> None:
    """Wrap ``interactive_ui._build_interactive_keyboard`` to add Settings.

    ``handle_interactive_ui`` resolves ``_build_interactive_keyboard`` from the
    module globals at call time, so patching the module attribute is enough.
    """
    global _installed
    if _installed:
        return

    # Lazy: interactive UI pulls in PTB types + the interactive package.
    from ccgram.handlers.interactive import interactive_ui as iu_mod

    original = iu_mod._build_interactive_keyboard

    def wrapped(window_id: str, ui_name: str = "", pane_id: str | None = None) -> Any:
        keyboard = original(window_id, ui_name=ui_name, pane_id=pane_id)
        if ui_name != "ExitPlanMode":
            return keyboard
        try:
            # Lazy: settings_panel installs alongside us; import at call time.
            from telegram import InlineKeyboardMarkup

            # Lazy: layer module deferred to the call path.
            from ..settings_panel import button_for_window

            rows = [list(row) for row in keyboard.inline_keyboard]
            rows.append([button_for_window(window_id)])
            return InlineKeyboardMarkup(rows)
        except Exception:  # noqa: BLE001 -- never break the real plan picker
            logger.debug("could not append Settings to plan keyboard", exc_info=True)
            return keyboard

    wrapped.__name__ = "_build_interactive_keyboard_with_settings"
    wrapped.__qualname__ = wrapped.__name__
    iu_mod._build_interactive_keyboard = wrapped  # type: ignore[assignment]
    _installed = True
    logger.info("ccgram-pro plan-approval surface installed — Settings on plan prompts")


def _reset_for_testing() -> None:
    global _installed
    _installed = False

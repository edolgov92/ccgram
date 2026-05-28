"""Plan-mode auto-entry.

When ``settings.defaults.plan_mode_on_new_session`` is true (the layer's
default), every fresh Claude window gets a Shift+Tab sent to it shortly
after launch so the operator's first prompt arrives in plan mode. Claude
then produces an ``ExitPlanMode`` tool call before doing anything
destructive — the upstream interactive UI handles the Approve / Edit
prompt over Telegram (we don't replace that flow).

This module wraps :func:`ccgram.handlers.topics.directory_callbacks._create_window_and_bind`
so post-creation we schedule an asynchronous "enter plan mode" task that
polls the pane for readiness, sends the key sequence, and verifies the
mode flipped.
"""

from .orchestrator import install_plan_mode_entry

__all__ = ["install_plan_mode_entry"]

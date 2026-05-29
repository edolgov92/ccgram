"""ccgram-pro Telegram command handlers.

Registered on the PTB Application from
:func:`ccgram_pro.extension.install` via :func:`install_layer_commands`.

Only commands with names that do NOT collide with the agent's own slash
commands live here. ``/model`` and ``/project`` were intentionally
removed: ``/model`` now falls through to ccgram's slash-command
forwarder so it reaches Claude Code's *native* model picker (the real
way to switch a running session's model), and project selection happens
in the directory picker at topic creation. ``/pr_fix`` / ``/pr_log`` are
unique to the layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from telegram.ext import Application

logger = structlog.get_logger()

_installed = False


def install_layer_commands(application: "Application") -> None:
    """Register every layer-owned slash command on *application*.

    Idempotent so extension.install can be re-run safely (the test
    harness exercises that path).
    """
    global _installed
    if _installed:
        return

    # Lazy: PTB types are only needed at register time.
    from telegram.ext import ApplicationHandlerStop, CommandHandler

    # Lazy: deferred to avoid a heavy/cyclic import at module load.
    from .pr_fix_command import pr_fix_command, pr_log_command

    def _stop_after(handler):  # noqa: ANN001, ANN202 -- thin PTB wrapper
        """Run *handler*, then raise ApplicationHandlerStop.

        Our CommandHandlers register in group -10 (before ccgram's
        group-0 COMMAND-forward MessageHandler). Stopping propagation
        keeps ccgram from also forwarding the command to the agent.
        """

        async def wrapped(update, context):  # noqa: ANN001, ANN202
            await handler(update, context)
            raise ApplicationHandlerStop

        return wrapped

    # PTB / Telegram command names: ``[a-z0-9_]{1,32}`` — no hyphens, so
    # the layer commands use snake_case. group=-10 so they win over
    # ccgram's COMMAND-forward fallback.
    application.add_handler(
        CommandHandler("pr_fix", _stop_after(pr_fix_command)), group=-10
    )
    application.add_handler(
        CommandHandler("pr_log", _stop_after(pr_log_command)), group=-10
    )

    _installed = True
    logger.info("ccgram-pro layer commands registered: /pr_fix /pr_log")


def _reset_for_testing() -> None:
    global _installed
    _installed = False

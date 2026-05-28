"""ccgram-pro Telegram command handlers.

Registered on the PTB Application from
:func:`ccgram_pro.extension.install` via
:func:`install_layer_commands`. Adding a new command is two steps:

1. Drop a module in this package exposing an ``async def`` handler.
2. Register it inside :func:`install_layer_commands`.
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
    from telegram.ext import CommandHandler

    from .model_command import model_command
    from .pr_fix_command import pr_fix_command, pr_log_command
    from .project_command import project_command

    # PTB / Telegram command names: ``[a-z0-9_]{1,32}``. No hyphens, so we
    # expose them under snake_case. Operator can alias in the BotFather
    # /setcommands if a different label is wanted.
    application.add_handler(CommandHandler("project", project_command))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("pr_fix", pr_fix_command))
    application.add_handler(CommandHandler("pr_log", pr_log_command))

    # The callback handlers piggy-back on the same dispatch hierarchy.
    from telegram.ext import CallbackQueryHandler

    from .project_command import project_picker_callback
    from .model_command import model_picker_callback

    application.add_handler(
        CallbackQueryHandler(
            project_picker_callback, pattern=r"^ccgrampro:project:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(model_picker_callback, pattern=r"^ccgrampro:model:")
    )

    _installed = True
    logger.info(
        "ccgram-pro layer commands registered: /project /model /pr-fix /pr-log"
    )


def _reset_for_testing() -> None:
    global _installed
    _installed = False

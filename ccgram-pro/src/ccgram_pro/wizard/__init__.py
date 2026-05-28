"""Interactive setup wizard for ccgram + ccgram-pro.

Public surface is :func:`run_wizard` — invoked by the ``ccgram-pro setup``
CLI subcommand. The wizard walks the operator through every configuration
needed to take the bot from a fresh checkout to a working install:

- Telegram bot token + allowed users
- Claude Code availability
- Predefined project list
- Optional LLM (shell mode + completion summaries)
- Optional voice transcription
- Optional Mini App
- Claude Code hook installation
- Doctor validation

Steps write to ``~/.ccgram/.env`` and ``~/.ccgram/layer/*.toml``
incrementally so a Ctrl-C mid-wizard preserves the work done so far.
"""

from .run import run_wizard

__all__ = ["run_wizard"]

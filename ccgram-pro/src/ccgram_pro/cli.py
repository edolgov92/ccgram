"""``ccgram-pro`` CLI entry — ``doctor`` + ``setup`` subcommands.

Kept deliberately thin: the layer itself runs inside ccgram via entry-point
dispatch. The CLI exists so users can validate the install and run the
guided setup wizard without booting the bot.
"""

from __future__ import annotations

import click

from . import __version__
from .doctor import run_doctor
from .wizard import run_wizard


@click.group(help="ccgram-pro maintenance commands.")
@click.version_option(__version__, prog_name="ccgram-pro")
def cli() -> None:
    """Top-level Click group — Click dispatches to subcommands."""


@cli.command(help="Validate layer configuration and dependencies.")
def doctor() -> None:
    rc = run_doctor()
    raise SystemExit(rc)


@cli.command(help="Interactive setup wizard for ccgram + ccgram-pro.")
def setup() -> None:
    rc = run_wizard()
    raise SystemExit(rc)


def main() -> None:
    """Entry-point declared in ``pyproject.toml`` (``ccgram-pro`` script)."""
    cli()


if __name__ == "__main__":
    main()

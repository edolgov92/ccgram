"""Wizard orchestration — drives the step modules in order.

Banner + summary live here so the steps stay focused on a single concern.
The whole flow takes the operator through about a dozen prompts; every
step writes its result immediately so a ``Ctrl-C`` mid-wizard preserves
what's been answered so far.
"""

from __future__ import annotations

import click

from .. import __version__
from . import steps


_STEP_FUNCTIONS = (
    steps.step_bot_token,
    steps.step_allowed_users,
    steps.step_claude_check,
    steps.step_projects,
    steps.step_llm,
    steps.step_whisper,
    steps.step_miniapp,
    steps.step_hooks,
    steps.step_doctors,
)


def _print_banner() -> None:
    click.secho(f"ccgram-pro {__version__} setup wizard", fg="cyan", bold=True)
    click.echo("Walks you through every config needed to run ccgram + ccgram-pro.")
    click.echo(
        "Answers are written incrementally to ~/.ccgram/.env and "
        "~/.ccgram/layer/projects.toml — Ctrl-C is safe."
    )


def _print_summary(state: steps.WizardState) -> None:
    click.echo()
    click.secho("━━ Summary ━━", fg="cyan", bold=True)
    rows = [
        ("Bot token", state.bot_token_set),
        ("Allowed users", state.allowed_users_set),
        ("Claude CLI detected", state.claude_present),
        ("Projects configured", bool(state.projects)),
        ("LLM provider", state.llm_configured),
        ("Voice transcription", state.whisper_configured),
        ("Mini App dashboard", state.miniapp_configured),
        ("Hooks installed", state.hooks_installed),
    ]
    for label, ok in rows:
        marker = (
            click.style("✓", fg="green") if ok else click.style("·", fg="bright_black")
        )
        click.echo(f"  {marker} {label}")

    click.echo()
    click.echo("Next steps:")
    click.echo(
        "  - Create a Telegram supergroup, enable Topics, and add the bot as admin."
    )
    click.echo(
        "  - Start the bot with `ccgram` (or your supervisor script);"
        " open a topic to begin."
    )
    if not state.hooks_installed and state.claude_present:
        click.echo("  - Run `ccgram hook --install` once you're ready (recommended).")
    if state.miniapp_configured:
        click.echo(
            "  - Point your reverse proxy at 127.0.0.1:8765 (or whatever you "
            "set CCGRAM_MINIAPP_PORT to)."
        )


def run_wizard() -> int:
    """Run the wizard end to end. Returns a shell-style exit code."""
    _print_banner()
    state = steps.initial_state()
    try:
        for step in _STEP_FUNCTIONS:
            step(state)
    except click.Abort:
        click.secho("\nWizard cancelled. Partial progress was saved.", fg="yellow")
        return 130
    _print_summary(state)
    return 0

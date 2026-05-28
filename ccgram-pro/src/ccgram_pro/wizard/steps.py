"""Wizard steps — each one is a small function the run loop drives.

Steps share these conventions:

- Take ``state: WizardState`` so they can read previous answers and side-
  effect the .env / TOML files.
- Use ``click.echo`` for headings and explanations; ``click.prompt`` /
  ``click.confirm`` for input.
- Return ``True`` when something was written, ``False`` when the user
  skipped. The orchestrator uses this for the final summary.

Each step writes immediately so a Ctrl-C mid-wizard preserves prior work.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import click
from ccgram.utils import ccgram_dir

from ..config import (
    Project,
    layer_dir,
    load_projects,
    projects_toml_path,
)
from .env import update_env
from .prompts import (
    LLM_PROVIDERS,
    WHISPER_PROVIDERS,
    is_valid_bot_token,
    is_valid_https_url,
    parse_user_ids,
)


@dataclass
class WizardState:
    """Mutable scratchpad threaded through every step."""

    env_path: Path
    projects_path: Path
    projects: list[Project] = field(default_factory=list)
    bot_token_set: bool = False
    allowed_users_set: bool = False
    claude_present: bool = False
    llm_configured: bool = False
    whisper_configured: bool = False
    miniapp_configured: bool = False
    hooks_installed: bool = False


def _heading(title: str) -> None:
    click.echo()
    click.secho(f"━━ {title} ━━", fg="cyan", bold=True)


def _hint(text: str) -> None:
    click.secho(text, fg="bright_black")


def step_bot_token(state: WizardState) -> None:
    _heading("Telegram bot token")
    _hint(
        "Create a bot via @BotFather on Telegram (send /newbot), then paste\n"
        "the token it gives you. Tokens look like ``123456789:ABC-DEF…``."
    )
    while True:
        value = click.prompt(
            "Bot token", hide_input=True, default="", show_default=False
        ).strip()
        if not value:
            click.secho("Bot token is required — aborting wizard.", fg="red")
            sys.exit(1)
        if is_valid_bot_token(value):
            update_env(state.env_path, {"TELEGRAM_BOT_TOKEN": value})
            state.bot_token_set = True
            click.secho("✓ Saved", fg="green")
            return
        click.secho(
            "That doesn't look like a Telegram bot token. Expected "
            "``<numeric id>:<35+ chars>``.",
            fg="red",
        )


def step_allowed_users(state: WizardState) -> None:
    _heading("Allowed Telegram users")
    _hint(
        "Comma-separated user IDs. Get yours from @userinfobot. The bot\n"
        "will refuse messages from anyone not on this list."
    )
    while True:
        value = click.prompt("Allowed user IDs", default="").strip()
        if not value:
            click.secho("At least one user id is required — aborting wizard.", fg="red")
            sys.exit(1)
        try:
            ids = parse_user_ids(value)
        except ValueError as exc:
            click.secho(f"{exc}", fg="red")
            continue
        update_env(state.env_path, {"ALLOWED_USERS": ",".join(str(i) for i in ids)})
        state.allowed_users_set = True
        click.secho(f"✓ Saved ({len(ids)} user(s))", fg="green")
        return


def step_claude_check(state: WizardState) -> None:
    _heading("Claude Code availability")
    claude_path = shutil.which("claude")
    if claude_path:
        click.secho(f"✓ claude CLI found at {claude_path}", fg="green")
        try:
            version = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if version.returncode == 0 and version.stdout.strip():
                _hint(f"  {version.stdout.strip()}")
        except subprocess.TimeoutExpired, FileNotFoundError:
            pass
        state.claude_present = True
        return
    click.secho("✗ claude CLI not on $PATH", fg="yellow")
    _hint(
        "Install Claude Code from https://docs.anthropic.com/claude-code, or\n"
        "set CCGRAM_CLAUDE_COMMAND to a wrapper that points at your install."
    )


def step_projects(state: WizardState) -> None:
    _heading("Predefined project list")
    _hint(
        "These appear in the /project picker once Phase 1 lands. Each\n"
        "session clones one of them into a per-session workspace.\n"
        "Leave blank when you're done adding projects."
    )
    existing = load_projects(state.projects_path)
    if existing:
        click.echo(f"Already configured: {len(existing)} project(s)")
        if not click.confirm("Add more?", default=False):
            state.projects = existing
            return

    collected: list[Project] = list(existing)
    while True:
        path_str = click.prompt(
            "Project path (absolute, blank to finish)", default=""
        ).strip()
        if not path_str:
            break
        path = Path(path_str).expanduser()
        if not path.is_dir():
            click.secho(f"  {path} is not a directory; please try again.", fg="red")
            continue
        label = click.prompt("Label shown in /project", default=path.name).strip()
        collected.append(
            Project(
                path=path,
                label=label or path.name,
                default_model="opus",
                default_reasoning="extra-high",
            )
        )

    if collected:
        _write_projects_toml(state.projects_path, collected)
        state.projects = collected
        click.secho(f"✓ Wrote {state.projects_path}", fg="green")
    else:
        _hint("(no projects configured)")


def step_llm(state: WizardState) -> None:
    _heading("Optional: LLM for shell mode + completion summaries")
    _hint(
        "ccgram uses an LLM for two things: turning natural-language into\n"
        "shell commands in shell-provider topics, and writing one-line\n"
        "Done summaries when an agent finishes. Skip if you don't want\n"
        "either."
    )
    if not click.confirm("Configure an LLM provider?", default=False):
        return
    provider = _pick("LLM provider", LLM_PROVIDERS)
    api_key = click.prompt(
        f"{provider} API key", hide_input=True, default="", show_default=False
    ).strip()
    if not api_key:
        click.secho("(no key entered — skipping LLM setup)", fg="yellow")
        return
    update_env(
        state.env_path,
        {"CCGRAM_LLM_PROVIDER": provider, "CCGRAM_LLM_API_KEY": api_key},
    )
    if click.confirm("Override default model?", default=False):
        model = click.prompt("Model name", default="").strip()
        if model:
            update_env(state.env_path, {"CCGRAM_LLM_MODEL": model})
    state.llm_configured = True
    click.secho("✓ LLM configured", fg="green")


def step_whisper(state: WizardState) -> None:
    _heading("Optional: voice transcription (Whisper)")
    _hint(
        "Enables sending voice messages — they get transcribed and queued\n"
        "with text input. Requires an OpenAI or Groq key (Groq's\n"
        "whisper-large-v3 endpoint is fast + cheap)."
    )
    if not click.confirm("Configure voice transcription?", default=False):
        return
    provider = _pick("Whisper provider", WHISPER_PROVIDERS)
    api_key = click.prompt(
        f"{provider} API key (blank to reuse OPENAI_API_KEY)",
        hide_input=True,
        default="",
        show_default=False,
    ).strip()
    update_env(state.env_path, {"CCGRAM_WHISPER_PROVIDER": provider})
    if api_key:
        update_env(state.env_path, {"CCGRAM_WHISPER_API_KEY": api_key})
    state.whisper_configured = True
    click.secho("✓ Voice transcription configured", fg="green")


def step_miniapp(state: WizardState) -> None:
    _heading("Optional: Mini App dashboard")
    _hint(
        "Enables the in-Telegram WebApp (live terminal, transcript search,\n"
        "diff viewer once Phase 5 lands). Needs an externally reachable\n"
        "HTTPS URL — set up cloudflared, caddy, or nginx pointing at\n"
        "http://127.0.0.1:8765 first."
    )
    if not click.confirm("Configure Mini App?", default=False):
        return
    while True:
        url = click.prompt("HTTPS base URL", default="").strip()
        if not url:
            click.secho("(no URL entered — skipping)", fg="yellow")
            return
        if is_valid_https_url(url):
            break
        click.secho("URL must start with https:// and include a host.", fg="red")
    updates = {"CCGRAM_MINIAPP_BASE_URL": url.rstrip("/")}
    if click.confirm("Override default host/port?", default=False):
        host = click.prompt("Host", default="127.0.0.1").strip()
        port = click.prompt("Port", default="8765").strip()
        updates["CCGRAM_MINIAPP_HOST"] = host
        updates["CCGRAM_MINIAPP_PORT"] = port
    update_env(state.env_path, updates)
    state.miniapp_configured = True
    click.secho("✓ Mini App configured", fg="green")


def step_hooks(state: WizardState) -> None:
    _heading("Claude Code hooks")
    _hint(
        "Installs the Claude Code hook scripts so ccgram gets instant\n"
        "events (Stop, Notification, SessionEnd…) instead of polling.\n"
        "Strongly recommended — most layer features depend on hooks."
    )
    if not state.claude_present:
        click.secho("Skipping (claude CLI not detected).", fg="yellow")
        return
    if not click.confirm("Run `ccgram hook --install`?", default=True):
        return
    ccgram_bin = shutil.which("ccgram")
    if ccgram_bin is None:
        click.secho("Cannot find ccgram on $PATH; install ccgram first.", fg="red")
        return
    result = subprocess.run(
        [ccgram_bin, "hook", "--install"], capture_output=True, text=True, check=False
    )
    if result.returncode == 0:
        click.secho("✓ Hooks installed", fg="green")
        state.hooks_installed = True
    else:
        click.secho(
            f"Hook install failed: {result.stderr.strip() or result.stdout.strip()}",
            fg="red",
        )


def step_doctors(_state: WizardState) -> None:
    """Run both doctors so the operator sees the same view as `ccgram-pro doctor`."""
    _heading("Verify")
    ccgram_bin = shutil.which("ccgram")
    if ccgram_bin is not None:
        click.echo("Running `ccgram doctor`…")
        subprocess.run([ccgram_bin, "doctor"], check=False)
    pro_bin = shutil.which("ccgram-pro")
    if pro_bin is not None:
        click.echo("\nRunning `ccgram-pro doctor`…")
        subprocess.run([pro_bin, "doctor"], check=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pick(label: str, options: tuple[str, ...]) -> str:
    click.echo(f"{label}:")
    for idx, name in enumerate(options, 1):
        click.echo(f"  {idx}) {name}")
    while True:
        raw = click.prompt("Choice", default="1").strip()
        try:
            choice = int(raw)
        except ValueError:
            click.secho("Pick a number.", fg="red")
            continue
        if 1 <= choice <= len(options):
            return options[choice - 1]
        click.secho(f"Pick a number 1..{len(options)}.", fg="red")


def _write_projects_toml(path: Path, projects: list[Project]) -> None:
    """Write *projects* as a fresh ``projects.toml``.

    Doesn't try to preserve existing content — the wizard owns this file
    when it runs. The TOML serializer is intentionally hand-rolled to
    avoid a runtime dependency on ``tomli-w`` and to keep the output
    diff-friendly (one ``[[project]]`` block per entry, in input order).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# ccgram-pro predefined project list — written by `ccgram-pro setup`.",
        "# Edit and restart ccgram; new entries appear in the /project keyboard.",
        "",
    ]
    for project in projects:
        lines.append("[[project]]")
        lines.append(f'path = "{project.path}"')
        lines.append(f'label = "{_escape_toml(project.label)}"')
        lines.append(f'default_model = "{_escape_toml(project.default_model)}"')
        lines.append(f'default_reasoning = "{_escape_toml(project.default_reasoning)}"')
        if project.default_preamble:
            lines.append(
                f'default_preamble = """{_escape_toml(project.default_preamble)}"""'
            )
        if project.install_command is not None:
            lines.append(f'install_command = "{_escape_toml(project.install_command)}"')
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _escape_toml(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def default_env_path() -> Path:
    return ccgram_dir() / ".env"


def default_projects_path() -> Path:
    return projects_toml_path()


def initial_state() -> WizardState:
    layer_dir().mkdir(parents=True, exist_ok=True)
    return WizardState(
        env_path=default_env_path(), projects_path=default_projects_path()
    )

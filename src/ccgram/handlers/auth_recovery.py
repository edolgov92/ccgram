"""Claude OAuth re-login driven from Telegram (auth recovery flow).

When a Claude Code session dies with an authentication failure (StopFailure
hook, ``authentication_failed``), the subscription's OAuth token has usually
been revoked — every session is dead until someone re-authenticates on the
server. This module removes the "ssh to the server" step: the bot drives
``claude`` → ``/login`` in a scratch tmux SESSION (never a window inside the
bot's own session — those get auto-adopted into Telegram topics), captures the
OAuth URL, and posts it to the affected topic. The user authorizes in a
browser and replies with the code; the flow types it into the TUI, verifies
``.credentials.json`` was repopulated, and confirms in the topic.

Key pieces:
  - ``is_auth_failure`` — pure classifier for StopFailure error text.
  - ``maybe_start_login_flow`` — debounced entry point (called by hook_events).
  - ``consume_login_code`` — text_handler intake for the OAuth code reply.
  - Pure helpers: ``classify_login_screen``, ``extract_oauth_url``,
    ``LOGIN_CODE_RE`` — unit-tested against real captured TUI screens.

Security: the OAuth code is fed to the login TUI and never logged; the
credentials file is only checked for presence/expiry, never read into logs.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from ..config import config

if TYPE_CHECKING:
    from telegram import Message

    from ..telegram_client import TelegramClient

logger = structlog.get_logger()

# Scratch tmux session for the login TUI. A SEPARATE session, not a window in
# the bot's own session: unbound windows there are auto-adopted into new
# Telegram topics (learned the hard way — a manual login window spawned a
# stray "__login__" topic).
LOGIN_TMUX_SESSION = "ccgram-login"

# OAuth code shape: <authorization_code>#<state>, both base64url-ish blobs.
LOGIN_CODE_RE = re.compile(r"[A-Za-z0-9_\-]{20,120}#[A-Za-z0-9_\-]{20,120}")

_URL_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "-._~:/?#@!$&'()*+,;=%"
)

_DEBOUNCE_SECONDS = 600.0  # one flow per 10 min, however many sessions fail
_CODE_TIMEOUT_SECONDS = 900.0  # user has 15 min to reply with the code
_DRIVE_TIMEOUT_ITERATIONS = 45  # ~45s for the TUI to reach the URL screen
_MAX_CODE_ATTEMPTS = 3


@dataclass
class LoginFlow:
    """One in-flight re-login: posted URL, awaiting the user's code reply."""

    client: "TelegramClient"
    user_id: int
    thread_id: int
    chat_id: int
    started_at: float
    url: str = ""
    awaiting_code: bool = False
    code: str = ""
    attempts: int = 0
    code_event: asyncio.Event = field(default_factory=asyncio.Event)


_flow: LoginFlow | None = None
_last_started: float = 0.0
_task: asyncio.Task | None = None


# ── Pure helpers ──────────────────────────────────────────────────────────


def is_auth_failure(error: str, error_details: str = "") -> bool:
    """True when a StopFailure error text is an authentication problem."""
    lowered = f"{error} {error_details}".lower()
    return (
        "authentication" in lowered
        or "unauthorized" in lowered
        or "invalid api key" in lowered
        or "oauth token" in lowered
        or "401" in lowered
    )


def classify_login_screen(pane_text: str) -> str:
    """Classify a captured login-TUI screen.

    Returns one of: ``success``, ``url_shown``, ``method_select``, ``trust``,
    ``prompt_ready``, ``unknown``. Order matters — later screens contain
    fragments of earlier ones.
    """
    if "Login successful" in pane_text or "Logged in as" in pane_text:
        return "success"
    if "claude.com/" in pane_text and (
        "Use the url below" in pane_text or "Paste code" in pane_text
    ):
        return "url_shown"
    if "Select login method" in pane_text:
        return "method_select"
    if "trust this folder" in pane_text or "Do you trust" in pane_text:
        return "trust"
    if "shift+tab to cycle" in pane_text or "? for shortcuts" in pane_text:
        return "prompt_ready"
    return "unknown"


def extract_oauth_url(pane_text: str) -> str:
    """Extract the OAuth URL from a captured pane, joining wrapped lines.

    The TUI wraps the long URL across terminal lines; newlines inside the URL
    region are skipped, and the first non-URL character (space, box glyph)
    terminates the scan.
    """
    start = pane_text.find("https://claude.com/")
    if start < 0:
        return ""
    chars: list[str] = []
    for ch in pane_text[start:]:
        if ch == "\n":
            continue
        if ch not in _URL_CHARS:
            break
        chars.append(ch)
    url = "".join(chars)
    return url if "oauth" in url else ""


# ── tmux plumbing (module-level so tests can monkeypatch) ─────────────────


async def _run_tmux(*args: str) -> tuple[int, str]:
    """Run a tmux command against the login session; (returncode, stdout)."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode or 0, stdout.decode("utf-8", errors="replace")


async def _capture_login_pane() -> str:
    _rc, out = await _run_tmux("capture-pane", "-p", "-J", "-t", LOGIN_TMUX_SESSION)
    return out


async def _kill_login_session() -> None:
    await _run_tmux("kill-session", "-t", LOGIN_TMUX_SESSION)


class LoginFlowError(RuntimeError):
    """The login TUI could not be driven to the expected screen."""


async def _drive_login_to_url() -> str:
    """Launch the login TUI in the scratch session and return the OAuth URL."""
    await _kill_login_session()
    rc, _ = await _run_tmux(
        "new-session", "-d", "-s", LOGIN_TMUX_SESSION, "-x", "220", "-y", "50"
    )
    if rc != 0:
        raise LoginFlowError("could not create login tmux session")
    await _run_tmux("send-keys", "-t", LOGIN_TMUX_SESSION, "claude", "Enter")

    sent_login = False
    last_state = "unknown"
    for _ in range(_DRIVE_TIMEOUT_ITERATIONS):
        await asyncio.sleep(1.0)
        pane_text = await _capture_login_pane()
        last_state = classify_login_screen(pane_text)
        if last_state == "url_shown":
            url = extract_oauth_url(pane_text)
            if url:
                return url
        elif last_state == "success":
            # Someone re-authenticated out-of-band while we were driving.
            return ""
        elif last_state == "trust":
            await _run_tmux("send-keys", "-t", LOGIN_TMUX_SESSION, "Enter")
        elif last_state == "method_select":
            # Option 1 (Claude subscription) is preselected.
            await _run_tmux("send-keys", "-t", LOGIN_TMUX_SESSION, "Enter")
        elif last_state == "prompt_ready" and not sent_login:
            sent_login = True
            await _run_tmux("send-keys", "-t", LOGIN_TMUX_SESSION, "/login", "Enter")
    raise LoginFlowError(f"login screen not reached (last state: {last_state})")


def _credentials_ok() -> bool:
    """True when the credentials file holds a live-looking OAuth token."""
    try:
        data = json.loads(
            (config.claude_config_dir / ".credentials.json").read_text(encoding="utf-8")
        )
    except OSError, ValueError:
        return False
    oauth = data.get("claudeAiOauth") or {}
    access_token = oauth.get("accessToken") or ""
    expires_at = oauth.get("expiresAt") or 0
    return bool(access_token) and expires_at > time.time() * 1000


# ── Flow lifecycle ────────────────────────────────────────────────────────


async def _notify(flow: LoginFlow, text: str) -> None:
    # Lazy: messaging_pipeline reaches back into handlers wiring.
    from .messaging_pipeline.message_sender import rate_limit_send_message

    try:
        await rate_limit_send_message(
            flow.client, flow.chat_id, text, message_thread_id=flow.thread_id
        )
    except Exception:  # noqa: BLE001 -- notification must not kill the flow
        logger.warning("auth-recovery notify failed", exc_info=True)


async def maybe_start_login_flow(
    client: "TelegramClient", user_id: int, thread_id: int, chat_id: int
) -> bool:
    """Start the re-login flow unless disabled, active, or debounced."""
    global _flow, _last_started, _task
    if not config.auto_relogin:
        return False
    now = time.monotonic()
    if _flow is not None:
        return False  # a flow is already running
    if now - _last_started < _DEBOUNCE_SECONDS:
        return False
    _last_started = now
    _flow = LoginFlow(
        client=client,
        user_id=user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        started_at=now,
    )
    _task = asyncio.create_task(_run_login_flow(_flow))
    logger.info("auth_recovery_started", user_id=user_id, thread_id=thread_id)
    return True


async def _run_login_flow(flow: LoginFlow) -> None:
    """Drive the whole flow: TUI → URL → user code → verify → confirm."""
    global _flow
    try:
        try:
            url = await _drive_login_to_url()
        except LoginFlowError as exc:
            await _notify(
                flow,
                "⚠️ Claude auth is down and the automatic re-login could not "
                f"start ({exc}). Log in manually: `claude` → `/login` on the "
                "server.",
            )
            return
        if not url and _credentials_ok():
            await _notify(flow, "✅ Claude auth is already restored.")
            return

        flow.url = url
        flow.awaiting_code = True
        await _notify(
            flow,
            "🔐 Claude auth needs a re-login.\n\n"
            "1. Open the link below and authorize.\n"
            "2. Reply HERE with the code you get.\n\n"
            f"{url}",
        )

        while flow.attempts < _MAX_CODE_ATTEMPTS:
            try:
                await asyncio.wait_for(
                    flow.code_event.wait(), timeout=_CODE_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                await _notify(
                    flow,
                    "⏳ Re-login expired (no code received in 15 min). "
                    "Send any message after the next auth error to retry.",
                )
                return
            flow.code_event.clear()
            flow.awaiting_code = False
            flow.attempts += 1

            await _run_tmux("send-keys", "-t", LOGIN_TMUX_SESSION, "-l", flow.code)
            await _run_tmux("send-keys", "-t", LOGIN_TMUX_SESSION, "Enter")

            success = False
            for _ in range(30):
                await asyncio.sleep(1.0)
                pane_text = await _capture_login_pane()
                if classify_login_screen(pane_text) == "success":
                    success = True
                    break
            if success and _credentials_ok():
                # Dismiss the "press Enter to continue" screen for cleanliness.
                await _run_tmux("send-keys", "-t", LOGIN_TMUX_SESSION, "Enter")
                await _notify(
                    flow,
                    "✅ Claude auth restored — sessions will recover on their "
                    "next message.",
                )
                logger.info("auth_recovery_succeeded", user_id=flow.user_id)
                return
            if flow.attempts < _MAX_CODE_ATTEMPTS:
                flow.awaiting_code = True
                await _notify(
                    flow,
                    "❌ That code didn't work — check it and reply once more.",
                )
        await _notify(
            flow,
            "❌ Re-login failed after several attempts. Log in manually: "
            "`claude` → `/login` on the server.",
        )
    finally:
        await _kill_login_session()
        _flow = None


async def consume_login_code(
    user_id: int, _thread_id: int | None, text: str, message: "Message"
) -> bool:
    """Text-handler intake: claim a message that is the OAuth code reply.

    Accepts the code from the affected user in ANY topic (they may reply
    wherever they saw the link) — the code shape is distinctive enough that
    false positives are not a realistic concern. Returns True when consumed.
    """
    flow = _flow
    if flow is None or not flow.awaiting_code or user_id != flow.user_id:
        return False
    code = text.strip()
    if not LOGIN_CODE_RE.fullmatch(code):
        return False
    flow.code = code
    flow.code_event.set()
    # Lazy: messaging_pipeline reaches back into handlers wiring.
    from .messaging_pipeline.message_sender import safe_reply

    await safe_reply(message, "⏳ Entering the code…")
    return True


def _reset_for_testing() -> None:
    global _flow, _last_started, _task
    _flow = None
    _last_started = 0.0
    _task = None

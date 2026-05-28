"""Validators + thin Click wrappers for the wizard prompts.

Keeping validation in a dedicated module lets the tests exercise the
regex / parsing logic without booting a Click runtime.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse


_BOT_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")


def is_valid_bot_token(value: str) -> bool:
    """Telegram bot tokens look like ``<numeric_id>:<35+ alphanumeric chars>``.

    @BotFather's real tokens are typically ``\\d{10}:[A-Za-z0-9_-]{35}`` but
    documenting the exact length is risky — we accept any token Telegram
    might mint while still rejecting obvious typos (missing colon, empty
    halves).
    """
    return bool(_BOT_TOKEN_RE.match(value.strip()))


def parse_user_ids(value: str) -> list[int]:
    """Parse ``"123, 456"`` into ``[123, 456]``. Raises ValueError on any garbage."""
    parts = [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
    if not parts:
        msg = "at least one Telegram user id is required"
        raise ValueError(msg)
    result: list[int] = []
    for p in parts:
        try:
            result.append(int(p))
        except ValueError as exc:
            raise ValueError(f"{p!r} is not an integer user id") from exc
    return result


def is_valid_https_url(value: str) -> bool:
    """Mini App base URL must be ``https://…`` with a host part.

    HTTP is rejected — Telegram WebApps refuse to load non-HTTPS pages, so
    accepting it would only produce a broken setup the user discovers later.
    """
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.netloc)


LLM_PROVIDERS: tuple[str, ...] = (
    "openai",
    "xai",
    "deepseek",
    "anthropic",
    "groq",
    "ollama",
)

WHISPER_PROVIDERS: tuple[str, ...] = ("openai", "groq")

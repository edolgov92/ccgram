"""URL helpers that combine the share store + token signer.

The bot's outbound message-building code uses :func:`make_share_url` to
get a clickable URL for a freshly minted share; the route handler uses
:func:`resolve_token` to go back from URL token to share record.
"""

from __future__ import annotations

import os

import structlog

from .store import ShareRecord, load_share
from .tokens import (
    DEFAULT_SHARE_TTL_SECONDS,
    InvalidShareToken,
    sign_share_token,
    verify_share_token,
)

logger = structlog.get_logger()


def _miniapp_base_url() -> str:
    """Resolve the operator-configured tunnel URL.

    Falls back to the ``_PENDING`` slot the silencer parks the URL in
    while ``Button_type_invalid`` is being worked around — share links
    are plain URL inline buttons, *not* WebApp buttons, so BotFather's
    domain registration is irrelevant for them.
    """
    return (
        os.environ.get("CCGRAM_MINIAPP_BASE_URL", "").strip()
        or os.environ.get("CCGRAM_MINIAPP_BASE_URL_PENDING", "").strip()
    )


def make_share_url(
    *,
    bot_token: str,
    share_id: str,
    ttl: int = DEFAULT_SHARE_TTL_SECONDS,
) -> str | None:
    """Mint a share token, build the URL. Returns ``None`` if no base URL set.

    The caller should treat ``None`` as "links are unavailable; don't
    attach the inline button" — the bot keeps working without them.
    """
    base = _miniapp_base_url()
    if not base:
        logger.debug("share URL not built — no CCGRAM_MINIAPP_BASE_URL")
        return None
    token = sign_share_token(bot_token=bot_token, share_id=share_id, ttl=ttl)
    return f"{base.rstrip('/')}/view/{token}"


def resolve_token(token: str, *, bot_token: str) -> ShareRecord:
    """Verify *token* and return the referenced share. Raises on failure."""
    payload = verify_share_token(token, bot_token=bot_token)
    return load_share(payload.share_id)


__all__ = [
    "DEFAULT_SHARE_TTL_SECONDS",
    "InvalidShareToken",
    "make_share_url",
    "resolve_token",
]

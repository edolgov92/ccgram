"""HMAC-signed share tokens — bound to a share_id with a 3-day default TTL.

Distinct from the window-scoped tokens in ``ccgram.miniapp.auth`` by a
``purpose`` claim (``"share"``) so the two token families cannot replay
each other even though both derive their signing key from the same bot
token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass


_TOKEN_NAMESPACE = b"ccgram-pro/share/v1"
DEFAULT_SHARE_TTL_SECONDS = 3 * 24 * 60 * 60  # 3 days
# Compose tokens authorise a state-changing web action (Open PR). They are
# minted only from a Telegram tap by the authenticated user and kept short-
# lived so a leaked link expires fast.
COMPOSE_PURPOSE = "compose"
DEFAULT_COMPOSE_TTL_SECONDS = 10 * 60  # 10 minutes


class InvalidShareToken(Exception):
    """Raised when a share token is malformed, expired, or signed wrong."""


@dataclass(frozen=True, slots=True)
class ShareTokenPayload:
    share_id: str
    exp: int
    purpose: str

    def is_expired(self, *, now: float | None = None) -> bool:
        ts = time.time() if now is None else now
        return ts >= self.exp


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(token: str) -> bytes:
    pad = "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(token + pad)


def _signing_key(bot_token: str) -> bytes:
    if not bot_token:
        raise InvalidShareToken("bot_token is empty")
    return hmac.new(
        _TOKEN_NAMESPACE, bot_token.encode("utf-8"), hashlib.sha256
    ).digest()


def _sign(
    *, bot_token: str, share_id: str, purpose: str, ttl: int, now: float | None
) -> str:
    if not share_id:
        raise InvalidShareToken("share_id is empty")
    issued = int(time.time() if now is None else now)
    payload = {"s": share_id, "exp": issued + int(ttl), "p": purpose}
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(_signing_key(bot_token), body, hashlib.sha256).digest()
    return f"{_b64url_encode(body)}.{_b64url_encode(sig)}"


def _verify(
    token: str, *, bot_token: str, expected_purpose: str, now: float | None
) -> ShareTokenPayload:
    if not token or token.count(".") != 1:
        raise InvalidShareToken("malformed token")
    body_b64, sig_b64 = token.split(".", 1)
    try:
        body = _b64url_decode(body_b64)
        sig = _b64url_decode(sig_b64)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise InvalidShareToken("base64 decode failed") from exc

    expected = hmac.new(_signing_key(bot_token), body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, sig):
        raise InvalidShareToken("signature mismatch")

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise InvalidShareToken("payload not JSON") from exc

    try:
        share_id = str(data["s"])
        exp = int(data["exp"])
        purpose = str(data["p"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidShareToken("payload missing fields") from exc

    if purpose != expected_purpose:
        raise InvalidShareToken(f"unexpected purpose: {purpose}")

    result = ShareTokenPayload(share_id=share_id, exp=exp, purpose=purpose)
    if result.is_expired(now=now):
        raise InvalidShareToken("token expired")
    return result


def sign_share_token(
    *,
    bot_token: str,
    share_id: str,
    ttl: int = DEFAULT_SHARE_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Mint a signed (read-only) share token for *share_id*."""
    return _sign(
        bot_token=bot_token, share_id=share_id, purpose="share", ttl=ttl, now=now
    )


def verify_share_token(
    token: str, *, bot_token: str, now: float | None = None
) -> ShareTokenPayload:
    """Verify a share token; raise :class:`InvalidShareToken` on any failure.

    Rejects compose tokens (wrong purpose) so a read link can never authorise a
    mutating action.
    """
    return _verify(token, bot_token=bot_token, expected_purpose="share", now=now)


def sign_compose_token(
    *,
    bot_token: str,
    window_id: str,
    ttl: int = DEFAULT_COMPOSE_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Mint a short-lived compose token bound to *window_id* (Open-PR auth)."""
    return _sign(
        bot_token=bot_token,
        share_id=window_id,
        purpose=COMPOSE_PURPOSE,
        ttl=ttl,
        now=now,
    )


def verify_compose_token(
    token: str, *, bot_token: str, now: float | None = None
) -> ShareTokenPayload:
    """Verify a compose token; rejects share tokens and expiry."""
    return _verify(
        token, bot_token=bot_token, expected_purpose=COMPOSE_PURPOSE, now=now
    )

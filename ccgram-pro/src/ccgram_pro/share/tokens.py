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


def sign_share_token(
    *,
    bot_token: str,
    share_id: str,
    ttl: int = DEFAULT_SHARE_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Mint a signed share token for *share_id*, valid for *ttl* seconds."""
    if not share_id:
        raise InvalidShareToken("share_id is empty")
    issued = int(time.time() if now is None else now)
    payload = {"s": share_id, "exp": issued + int(ttl), "p": "share"}
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(_signing_key(bot_token), body, hashlib.sha256).digest()
    return f"{_b64url_encode(body)}.{_b64url_encode(sig)}"


def verify_share_token(
    token: str,
    *,
    bot_token: str,
    now: float | None = None,
) -> ShareTokenPayload:
    """Verify a share token; return the decoded payload.

    Raises :class:`InvalidShareToken` on any failure (bad format, bad
    signature, wrong purpose claim, or expired).
    """
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

    if purpose != "share":
        raise InvalidShareToken(f"unexpected purpose: {purpose}")

    result = ShareTokenPayload(share_id=share_id, exp=exp, purpose=purpose)
    if result.is_expired(now=now):
        raise InvalidShareToken("token expired")
    return result

"""Single-use CSRF nonces for the web composer's state-changing POST.

A compose token authorises *who* may open the composer (only the Telegram
user who tapped the button). The nonce additionally protects the POST from
cross-site form submission and replay: the GET form embeds a fresh nonce
bound to the window, and the POST consumes it exactly once.

In-memory is sufficient — the Mini App runs in the bot's single process, and a
restart simply invalidates any pending form (safe: the user reopens it).
"""

from __future__ import annotations

import secrets
import time

# nonce -> (window_id, expiry_epoch)
_nonces: dict[str, tuple[str, float]] = {}
_DEFAULT_TTL_SECONDS = 600.0
_MAX_NONCES = 512


def mint_nonce(window_id: str, *, ttl: float = _DEFAULT_TTL_SECONDS) -> str:
    """Mint a single-use nonce bound to *window_id*."""
    _prune()
    nonce = secrets.token_urlsafe(16)
    _nonces[nonce] = (window_id, time.time() + ttl)
    return nonce


def consume_nonce(nonce: str, window_id: str) -> bool:
    """Consume *nonce*; True iff it was valid, unexpired, and bound to window."""
    entry = _nonces.pop(nonce, None)
    if entry is None:
        return False
    bound_window, expiry = entry
    if time.time() >= expiry:
        return False
    return bound_window == window_id


def _prune() -> None:
    now = time.time()
    expired = [n for n, (_w, exp) in _nonces.items() if now >= exp]
    for n in expired:
        _nonces.pop(n, None)
    # Hard cap so a flood of GETs without POSTs can't grow the map unbounded.
    if len(_nonces) > _MAX_NONCES:
        for n in sorted(_nonces, key=lambda k: _nonces[k][1])[
            : len(_nonces) - _MAX_NONCES
        ]:
            _nonces.pop(n, None)


def _reset_for_testing() -> None:
    _nonces.clear()

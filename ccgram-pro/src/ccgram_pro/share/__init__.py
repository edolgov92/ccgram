"""Share-link store + token signer for long-view URLs.

When a Telegram topic posts a short user-friendly reply, the layer
attaches a "View full turn" inline button pointing at a signed
``/view/<token>`` URL on the cloudflared tunnel. The token references a
share record on disk under ``<layer_dir>/shares/<share_id>/`` containing
the full assistant turn (or any other long-form content the layer wants
to expose).

Tokens reuse the HMAC primitive in ``ccgram.miniapp.auth`` (bot-token
derived key, base64url(payload).base64url(sig)) but carry a distinct
``purpose`` claim so window-scoped tokens cannot be replayed as share
tokens and vice versa.
"""

from .store import (
    ShareNotFound,
    ShareRecord,
    load_share,
    prune_expired,
    save_share,
)
from .tokens import InvalidShareToken, sign_share_token, verify_share_token

__all__ = [
    "InvalidShareToken",
    "ShareNotFound",
    "ShareRecord",
    "load_share",
    "prune_expired",
    "save_share",
    "sign_share_token",
    "verify_share_token",
]

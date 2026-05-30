from __future__ import annotations

import pytest
from ccgram_pro.share.tokens import (
    InvalidShareToken,
    sign_compose_token,
    sign_share_token,
    verify_compose_token,
    verify_share_token,
)

BOT = "12345:abcdef"


def test_compose_token_roundtrip() -> None:
    tok = sign_compose_token(bot_token=BOT, window_id="@7")
    payload = verify_compose_token(tok, bot_token=BOT)
    assert payload.share_id == "@7"
    assert payload.purpose == "compose"


def test_share_token_rejected_as_compose() -> None:
    tok = sign_share_token(bot_token=BOT, share_id="@7")
    with pytest.raises(InvalidShareToken):
        verify_compose_token(tok, bot_token=BOT)


def test_compose_token_rejected_as_share() -> None:
    tok = sign_compose_token(bot_token=BOT, window_id="@7")
    with pytest.raises(InvalidShareToken):
        verify_share_token(tok, bot_token=BOT)


def test_compose_token_expiry() -> None:
    tok = sign_compose_token(bot_token=BOT, window_id="@7", ttl=10, now=1000.0)
    with pytest.raises(InvalidShareToken):
        verify_compose_token(tok, bot_token=BOT, now=1011.0)


def test_compose_token_wrong_bot_token() -> None:
    tok = sign_compose_token(bot_token=BOT, window_id="@7")
    with pytest.raises(InvalidShareToken):
        verify_compose_token(tok, bot_token="other")

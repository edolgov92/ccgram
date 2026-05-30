from __future__ import annotations

import pytest
from ccgram_pro.share import csrf


@pytest.fixture(autouse=True)
def _reset():
    csrf._reset_for_testing()
    yield
    csrf._reset_for_testing()


def test_mint_and_consume_once() -> None:
    nonce = csrf.mint_nonce("@5")
    assert csrf.consume_nonce(nonce, "@5") is True
    # single-use
    assert csrf.consume_nonce(nonce, "@5") is False


def test_consume_wrong_window() -> None:
    nonce = csrf.mint_nonce("@5")
    assert csrf.consume_nonce(nonce, "@6") is False


def test_consume_unknown_nonce() -> None:
    assert csrf.consume_nonce("nope", "@5") is False


def test_expired_nonce() -> None:
    nonce = csrf.mint_nonce("@5", ttl=-1.0)
    assert csrf.consume_nonce(nonce, "@5") is False

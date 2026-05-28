"""Tests for ``ccgram_pro.wizard.prompts`` validators."""

from __future__ import annotations

import pytest
from ccgram_pro.wizard.prompts import (
    is_valid_bot_token,
    is_valid_https_url,
    parse_user_ids,
)


@pytest.mark.parametrize(
    "token",
    [
        "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        "987654321:A" + "x" * 34,
        "100000:" + "0" * 35,
    ],
)
def test_valid_bot_tokens(token: str) -> None:
    assert is_valid_bot_token(token)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "no-colon",
        "short:abc",
        "12345:notlongenoughbyfar",
        "abc:" + "x" * 35,  # numeric id missing
        ":" + "x" * 35,  # empty id
    ],
)
def test_invalid_bot_tokens(token: str) -> None:
    assert not is_valid_bot_token(token)


def test_parse_user_ids_single() -> None:
    assert parse_user_ids("12345") == [12345]


def test_parse_user_ids_multiple() -> None:
    assert parse_user_ids("1, 2, 3") == [1, 2, 3]


def test_parse_user_ids_accepts_semicolons() -> None:
    assert parse_user_ids("1; 2; 3") == [1, 2, 3]


def test_parse_user_ids_rejects_empty() -> None:
    with pytest.raises(ValueError, match="at least one"):
        parse_user_ids("")


def test_parse_user_ids_rejects_non_numeric() -> None:
    with pytest.raises(ValueError, match="not an integer"):
        parse_user_ids("1, abc, 3")


def test_parse_user_ids_strips_whitespace_only_entries() -> None:
    assert parse_user_ids("1,,2") == [1, 2]


def test_https_url_accepts_real_urls() -> None:
    assert is_valid_https_url("https://example.com")
    assert is_valid_https_url("https://example.com/path?q=1")
    assert is_valid_https_url("https://tunnel-abc.example.dev")


def test_https_url_rejects_http() -> None:
    assert not is_valid_https_url("http://example.com")


def test_https_url_rejects_no_host() -> None:
    assert not is_valid_https_url("https://")


def test_https_url_rejects_garbage() -> None:
    assert not is_valid_https_url("")
    assert not is_valid_https_url("not a url")
    assert not is_valid_https_url("ftp://example.com")

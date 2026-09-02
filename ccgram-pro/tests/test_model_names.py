"""Tests for the shared model display-name mapping."""

from __future__ import annotations

import pytest

from ccgram_pro.model_names import display_name


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-fable-5", "Fable 5"),
        ("claude-opus-5", "Opus 5"),
        ("claude-opus-4-8", "Opus 4.8"),
        ("claude-opus-4-8[1m]", "Opus 4.8"),
        ("claude-sonnet-5", "Sonnet 5"),
        ("claude-haiku-4-5", "Haiku 4.5"),
        ("fable5", "Fable 5"),
        ("fable5-1m", "Fable 5"),
        ("claude-fable-5-1", "Fable 5.1"),
        ("claude-fable-5-1[1m]", "Fable 5.1"),
        ("fable51", "Fable 5.1"),
        ("fable51-1m", "Fable 5.1"),
        ("opus5", "Opus 5"),
        ("opus5-1m", "Opus 5"),
        ("opus48", "Opus 4.8"),
        ("opus48-1m", "Opus 4.8"),
    ],
)
def test_known_models(model: str, expected: str) -> None:
    assert display_name(model) == expected


def test_empty_is_blank() -> None:
    assert display_name("") == ""
    assert display_name(None) == ""


def test_unknown_family_degrades_gracefully() -> None:
    assert display_name("claude-nova-9") == "Nova 9"
    assert display_name("some-future-model") == "Some Future Model"

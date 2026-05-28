"""Tests for ``ccgram_pro.wizard.env`` — .env reader/writer round-trip."""

from __future__ import annotations

from pathlib import Path

from ccgram_pro.wizard.env import read_env, update_env


def test_read_env_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_env(tmp_path / "nope.env") == {}


def test_read_env_parses_simple_assignments(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("FOO=bar\nBAZ=qux\n")
    assert read_env(f) == {"FOO": "bar", "BAZ": "qux"}


def test_read_env_ignores_comments_and_blank_lines(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("# header\n\nKEY=value\n# trailing\n")
    assert read_env(f) == {"KEY": "value"}


def test_read_env_strips_surrounding_quotes(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("KEY=\"quoted value\"\nOTHER='single'\n")
    assert read_env(f) == {"KEY": "quoted value", "OTHER": "single"}


def test_update_env_creates_file_when_missing(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    update_env(f, {"TELEGRAM_BOT_TOKEN": "1:abc"})
    assert "TELEGRAM_BOT_TOKEN=1:abc" in f.read_text()


def test_update_env_preserves_unknown_keys_and_comments(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    original = (
        "# my notes\n"
        "EXTERNAL_KEY=keepme\n"
        "TELEGRAM_BOT_TOKEN=oldtoken\n"
        "# trailing comment\n"
    )
    f.write_text(original)
    update_env(f, {"TELEGRAM_BOT_TOKEN": "newtoken"})
    out = f.read_text()
    assert "# my notes" in out
    assert "EXTERNAL_KEY=keepme" in out
    assert "TELEGRAM_BOT_TOKEN=newtoken" in out
    assert "TELEGRAM_BOT_TOKEN=oldtoken" not in out
    assert "# trailing comment" in out


def test_update_env_appends_new_keys_with_section_header(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("EXISTING=1\n")
    update_env(f, {"NEW_KEY": "value"})
    out = f.read_text()
    assert "EXISTING=1" in out
    assert "# Added by ccgram-pro setup" in out
    assert "NEW_KEY=value" in out


def test_update_env_quotes_values_with_spaces(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    update_env(f, {"PREAMBLE": "hello world"})
    assert 'PREAMBLE="hello world"' in f.read_text()


def test_update_env_bare_format_for_safe_values(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    update_env(f, {"TOKEN": "1234:Abc-Def_123"})
    assert "TOKEN=1234:Abc-Def_123" in f.read_text()


def test_update_env_escapes_embedded_quotes_and_backslashes(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    update_env(f, {"WEIRD": 'one "two" \\three'})
    raw = f.read_text()
    # The format_value escapes \ → \\ and " → \"
    assert 'WEIRD="one \\"two\\" \\\\three"' in raw


def test_update_env_empty_string_value(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    update_env(f, {"BLANK": ""})
    assert "BLANK=" in f.read_text()


def test_update_env_atomic_no_temp_files_left(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    update_env(f, {"K": "v"})
    siblings = [p for p in tmp_path.iterdir() if p.name != ".env"]
    assert siblings == []


def test_update_env_round_trip(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    update_env(f, {"A": "1", "B": "two words", "C": "x"})
    parsed = read_env(f)
    assert parsed == {"A": "1", "B": "two words", "C": "x"}

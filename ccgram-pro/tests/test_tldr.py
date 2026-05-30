from __future__ import annotations

from ccgram_pro.output_pipeline import tldr


def test_extract_single_block() -> None:
    text = f"Did the work.\n{tldr.TLDR_OPEN}\nFixed the bug.\n{tldr.TLDR_CLOSE}"
    assert tldr.extract_tldr(text) == "Fixed the bug."


def test_extract_takes_last_of_multiple() -> None:
    text = (
        f"{tldr.TLDR_OPEN}first{tldr.TLDR_CLOSE}\n"
        f"{tldr.TLDR_OPEN}second{tldr.TLDR_CLOSE}"
    )
    assert tldr.extract_tldr(text) == "second"


def test_extract_returns_none_when_absent() -> None:
    assert tldr.extract_tldr("just a normal response") is None


def test_extract_returns_none_when_empty_block() -> None:
    assert tldr.extract_tldr(f"{tldr.TLDR_OPEN}   {tldr.TLDR_CLOSE}") is None


def test_strip_removes_block_keeps_detail() -> None:
    text = f"Technical detail here.\n{tldr.TLDR_OPEN}\nsummary\n{tldr.TLDR_CLOSE}"
    stripped = tldr.strip_tldr(text)
    assert "Technical detail here." in stripped
    assert tldr.TLDR_OPEN not in stripped
    assert "summary" not in stripped


def test_strip_handles_no_block() -> None:
    assert tldr.strip_tldr("plain") == "plain"


def test_strip_handles_multiple_blocks() -> None:
    text = f"a{tldr.TLDR_OPEN}x{tldr.TLDR_CLOSE}b{tldr.TLDR_OPEN}y{tldr.TLDR_CLOSE}"
    assert "x" not in tldr.strip_tldr(text)
    assert "y" not in tldr.strip_tldr(text)


def test_system_prompt_contains_both_markers() -> None:
    assert tldr.TLDR_OPEN in tldr.TLDR_SYSTEM_PROMPT
    assert tldr.TLDR_CLOSE in tldr.TLDR_SYSTEM_PROMPT

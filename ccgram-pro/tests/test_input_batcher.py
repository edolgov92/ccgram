"""Tests for ``ccgram_pro.input_pipeline.batcher`` — enqueue / flush / clear."""

from __future__ import annotations

import pytest
from ccgram_pro import state
from ccgram_pro.input_pipeline.batcher import (
    clear,
    enqueue,
    flush,
    pending_count,
)


@pytest.fixture
def sidecar():
    """A fresh sidecar with batch_mode + silent_mode default-on."""
    state.save(
        state.WindowSidecar(window_id="@b", window_creation_epoch=0.0)
    )
    return state.load("@b")


async def test_enqueue_appends_text(sidecar) -> None:
    total, _ = await enqueue("@b", kind="text", body="hello")
    assert total == 1
    sc = state.load("@b")
    assert len(sc.current_batch) == 1
    assert sc.current_batch[0].kind == "text"
    assert sc.current_batch[0].body == "hello"


async def test_enqueue_counts_total(sidecar) -> None:
    total1, _ = await enqueue("@b", kind="text", body="one")
    total2, _ = await enqueue("@b", kind="voice", body="two")
    total3, _ = await enqueue("@b", kind="text", body="three")
    assert (total1, total2, total3) == (1, 2, 3)


async def test_enqueue_rejects_unknown_kind(sidecar) -> None:
    with pytest.raises(ValueError, match="unknown batch item kind"):
        await enqueue("@b", kind="bogus", body="x")


def test_pending_count_zero_when_no_sidecar() -> None:
    assert pending_count("@nope") == 0


async def test_pending_count_reflects_state(sidecar) -> None:
    await enqueue("@b", kind="text", body="a")
    await enqueue("@b", kind="text", body="b")
    assert pending_count("@b") == 2


async def test_flush_combines_text_items(sidecar) -> None:
    await enqueue("@b", kind="text", body="first")
    await enqueue("@b", kind="text", body="second")
    result = await flush("@b")
    assert result is not None
    assert "first" in result.combined_text
    assert "second" in result.combined_text
    assert result.item_count == 2
    assert state.load("@b").current_batch == []


async def test_flush_includes_preamble_first_time_only(sidecar) -> None:
    await enqueue("@b", kind="text", body="hi")
    first = await flush("@b")
    assert first is not None
    assert first.preamble_included is True

    await enqueue("@b", kind="text", body="again")
    second = await flush("@b")
    assert second is not None
    assert second.preamble_included is False
    # The second flush shouldn't have the preamble preamble-text in it.
    from ccgram_pro.config import load_settings

    preamble = load_settings().defaults.preamble
    assert preamble in first.combined_text
    assert preamble not in second.combined_text


async def test_flush_marks_voice_items(sidecar) -> None:
    await enqueue("@b", kind="text", body="text part")
    await enqueue("@b", kind="voice", body="voice transcript")
    result = await flush("@b")
    assert result is not None
    assert "[voice]: voice transcript" in result.combined_text
    # Voice note appears once even with multiple voice items.
    await enqueue("@b", kind="voice", body="v1")
    await enqueue("@b", kind="voice", body="v2")
    result2 = await flush("@b")
    assert result2 is not None
    assert result2.combined_text.count("transcription errors") == 1


async def test_flush_returns_none_on_empty(sidecar) -> None:
    assert await flush("@b") is None


async def test_clear_drops_pending(sidecar) -> None:
    await enqueue("@b", kind="text", body="x")
    await enqueue("@b", kind="text", body="y")
    removed = await clear("@b")
    assert removed == 2
    assert state.load("@b").current_batch == []


async def test_clear_zero_when_nothing_pending(sidecar) -> None:
    assert await clear("@b") == 0


async def test_clear_does_not_reset_preamble_sent(sidecar) -> None:
    """Clearing buffered items must not give back the first-send preamble."""
    await enqueue("@b", kind="text", body="hi")
    await flush("@b")
    sc_after_flush = state.load("@b")
    assert sc_after_flush.preamble_sent is True
    await enqueue("@b", kind="text", body="more")
    await clear("@b")
    sc_after_clear = state.load("@b")
    assert sc_after_clear.preamble_sent is True


async def test_skips_empty_bodies(sidecar) -> None:
    await enqueue("@b", kind="text", body="real")
    await enqueue("@b", kind="text", body="   ")  # whitespace
    await enqueue("@b", kind="text", body="")
    result = await flush("@b")
    assert result is not None
    # Composed text contains the real body but not the whitespace ones.
    assert "real" in result.combined_text
    parts = result.combined_text.split("\n\n")
    # preamble + "real" = 2 paragraphs
    assert len([p for p in parts if p.strip() == "real"]) == 1

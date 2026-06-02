from __future__ import annotations

from types import SimpleNamespace

import pytest
from ccgram_pro.input_pipeline import batcher, file_batch
from telegram.ext import ApplicationHandlerStop


def test_clean_caption_keeps_newlines_strips_control() -> None:
    assert file_batch._clean_caption("line1\nline2\x00\x07") == "line1\nline2"
    assert file_batch._clean_caption("  hi  ") == "hi"
    # No 500-char clamp: long captions survive intact.
    long = "x" * 1200
    assert file_batch._clean_caption(long) == long


def _setup(monkeypatch, *, batched: bool, window: str | None = "@5") -> None:
    import ccgram.config as cfgmod

    monkeypatch.setattr(cfgmod.config, "is_user_allowed", lambda uid: True)
    import ccgram.handlers.callback_helpers as ch

    monkeypatch.setattr(ch, "get_thread_id", lambda update: 2)
    import ccgram.thread_router as tr

    monkeypatch.setattr(
        tr,
        "thread_router",
        SimpleNamespace(resolve_window_for_thread=lambda u, t: window),
    )
    monkeypatch.setattr(file_batch.intercept, "_is_batched", lambda wid: batched)


def _photo_update():
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        message=SimpleNamespace(
            photo=[SimpleNamespace(file_id="f", file_unique_id="u", file_size=10)],
            document=None,
            caption="note",
            chat=SimpleNamespace(id=10),
            get_bot=lambda: SimpleNamespace(),
        ),
    )


async def test_passthrough_when_not_batched(monkeypatch) -> None:
    _setup(monkeypatch, batched=False)
    called: list[int] = []

    async def _stub(*a, **k) -> None:
        called.append(1)

    monkeypatch.setattr(file_batch, "_save_and_enqueue", _stub)
    # Must NOT raise — falls through to ccgram's immediate-upload handler.
    await file_batch.handle_photo(_photo_update(), SimpleNamespace())
    assert called == []


async def test_passthrough_when_unbound(monkeypatch) -> None:
    _setup(monkeypatch, batched=True, window=None)
    called: list[int] = []

    async def _stub(*a, **k) -> None:
        called.append(1)

    monkeypatch.setattr(file_batch, "_save_and_enqueue", _stub)
    await file_batch.handle_photo(_photo_update(), SimpleNamespace())
    assert called == []


async def test_batches_photo_when_batched(monkeypatch) -> None:
    _setup(monkeypatch, batched=True)
    captured: list[tuple[str, bool]] = []

    async def _stub(message, user_id, thread_id, window_id, *, is_photo) -> None:  # noqa: ANN001
        captured.append((window_id, is_photo))

    monkeypatch.setattr(file_batch, "_save_and_enqueue", _stub)
    with pytest.raises(ApplicationHandlerStop):
        await file_batch.handle_photo(_photo_update(), SimpleNamespace())
    assert captured == [("@5", True)]


async def test_unauthorized_passes_through(monkeypatch) -> None:
    _setup(monkeypatch, batched=True)
    import ccgram.config as cfgmod

    monkeypatch.setattr(cfgmod.config, "is_user_allowed", lambda uid: False)
    called: list[int] = []

    async def _stub(*a, **k) -> None:
        called.append(1)

    monkeypatch.setattr(file_batch, "_save_and_enqueue", _stub)
    await file_batch.handle_photo(_photo_update(), SimpleNamespace())
    assert called == []


async def test_batcher_accepts_file_kind_and_composes() -> None:
    await batcher.enqueue(
        "@f", kind="file", body="I've uploaded an image to .ccgram-uploads/x.png"
    )
    result = await batcher.flush("@f")
    assert result is not None
    assert ".ccgram-uploads/x.png" in result.combined_text

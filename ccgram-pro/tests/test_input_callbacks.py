from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ccgram_pro.input_pipeline import callbacks, intercept


class _Msg:
    def __init__(self) -> None:
        self.deleted = False
        self.edited: str | None = None
        self.chat = SimpleNamespace(id=999)
        self.message_thread_id = 100

    async def delete(self) -> None:
        self.deleted = True

    async def edit_text(self, text: str = "", **_kw: Any) -> None:
        self.edited = text


class _Query:
    def __init__(self, data: str = "ccgrampro:batch:flush:@5") -> None:
        self.data = data
        self.message = _Msg()
        self.answers: list[str] = []

    async def answer(self, text: str = "", **_kw: Any) -> None:
        self.answers.append(text)


def _ctx() -> Any:
    return SimpleNamespace(bot=SimpleNamespace())


async def test_flush_success_deletes_status_with_no_notification(monkeypatch) -> None:
    forwarded: list[str] = []

    async def stub_flush(window_id: str):
        return SimpleNamespace(combined_text="hi", item_count=2, preamble_included=True)

    async def stub_forward(window_id, user_id, thread_id, text, client, message):  # noqa: ANN001, ARG001
        forwarded.append(text)

    monkeypatch.setattr(callbacks, "flush", stub_flush)
    monkeypatch.setattr(intercept, "_ORIGINAL_FORWARD_MESSAGE", stub_forward)
    q = _Query()
    await callbacks._do_flush(q, 7, 100, "@5", _ctx())
    assert forwarded == ["hi"]
    assert q.message.deleted is True
    assert q.message.edited is None  # no lingering "Sent N items" message
    assert q.answers == [""]  # silent ack, no toast text


async def test_flush_starts_bubble_even_when_callback_expired(monkeypatch) -> None:
    from telegram.error import BadRequest

    forwarded: list[str] = []
    bubble_started: list[str] = []

    async def stub_flush(window_id: str):
        return SimpleNamespace(combined_text="hi", item_count=2, preamble_included=True)

    async def stub_forward(window_id, user_id, thread_id, text, client, message):  # noqa: ANN001, ARG001
        forwarded.append(text)

    async def stub_begin(**kwargs: Any) -> None:
        bubble_started.append(kwargs["window_id"])

    monkeypatch.setattr(callbacks, "flush", stub_flush)
    monkeypatch.setattr(intercept, "_ORIGINAL_FORWARD_MESSAGE", stub_forward)
    monkeypatch.setattr(
        "ccgram_pro.output_pipeline.progress_bubble.begin_for_turn", stub_begin
    )

    class _ExpiredQuery(_Query):
        async def answer(self, text: str = "", **_kw: Any) -> None:
            raise BadRequest("Query is too old and response timeout expired")

    q = _ExpiredQuery()
    # Must NOT raise, and the bubble must still start despite the dead callback.
    await callbacks._do_flush(q, 7, 100, "@5", _ctx())
    assert forwarded == ["hi"]
    assert bubble_started == ["@5"]


async def test_flush_failure_keeps_message_and_alerts(monkeypatch) -> None:
    async def stub_flush(window_id: str):
        return SimpleNamespace(
            combined_text="hi", item_count=1, preamble_included=False
        )

    async def stub_forward(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(callbacks, "flush", stub_flush)
    monkeypatch.setattr(intercept, "_ORIGINAL_FORWARD_MESSAGE", stub_forward)
    q = _Query()
    await callbacks._do_flush(q, 7, 100, "@5", _ctx())
    assert q.message.deleted is False  # kept so the user can retry
    assert any("failed" in a.lower() for a in q.answers)


async def test_empty_batch_deletes_status(monkeypatch) -> None:
    async def stub_flush(window_id: str):
        return None

    monkeypatch.setattr(callbacks, "flush", stub_flush)
    q = _Query()
    await callbacks._do_flush(q, 7, 100, "@5", _ctx())
    assert q.message.deleted is True
    assert q.message.edited is None


async def test_clear_deletes_status_with_ephemeral_toast(monkeypatch) -> None:
    async def stub_clear(window_id: str):
        return 3

    monkeypatch.setattr(callbacks, "clear", stub_clear)
    q = _Query(data="ccgrampro:batch:clear:@5")
    await callbacks._do_clear(q, 7, 100)
    assert q.message.deleted is True
    assert q.answers and "Cleared 3" in q.answers[0]

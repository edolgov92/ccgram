from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from ccgram_pro.input_pipeline import intercept


@pytest.fixture(autouse=True)
def _clear_status():
    intercept._status_messages.clear()
    yield
    intercept._status_messages.clear()


class _Bot:
    def __init__(self, *, fail_send: bool = False) -> None:
        self.fail_send = fail_send
        self.sent: list[dict[str, Any]] = []
        self.deleted: list[int] = []
        self._next_id = 100

    async def send_message(self, **kwargs: Any) -> Any:
        from telegram.error import TelegramError

        if self.fail_send:
            raise TelegramError("boom")
        self.sent.append(kwargs)
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self.deleted.append(message_id)


def test_status_text_wording() -> None:
    assert "1 message batched" in intercept._status_text(1)
    assert "2 messages batched" in intercept._status_text(2)
    assert "Send" in intercept._status_text(1)


async def test_first_status_no_delete() -> None:
    bot = _Bot()
    await intercept._edit_or_send_status(
        bot=bot, chat_id=10, thread_id=2, user_id=7, window_id="@1", count=1
    )
    assert len(bot.sent) == 1
    assert bot.deleted == []
    assert intercept._status_messages[(7, 2)] == 101


async def test_status_repost_deletes_prior_and_sends_fresh() -> None:
    bot = _Bot()
    intercept._status_messages[(7, 2)] = 55
    await intercept._edit_or_send_status(
        bot=bot, chat_id=10, thread_id=2, user_id=7, window_id="@1", count=2
    )
    assert len(bot.sent) == 1
    assert bot.deleted == [55]
    assert intercept._status_messages[(7, 2)] == 101


async def test_status_send_failure_keeps_prior_id() -> None:
    bot = _Bot(fail_send=True)
    intercept._status_messages[(7, 2)] = 55
    await intercept._edit_or_send_status(
        bot=bot, chat_id=10, thread_id=2, user_id=7, window_id="@1", count=2
    )
    assert bot.sent == []
    assert bot.deleted == []
    assert intercept._status_messages[(7, 2)] == 55


async def test_status_posts_to_thread_silently() -> None:
    bot = _Bot()
    await intercept._edit_or_send_status(
        bot=bot, chat_id=10, thread_id=2, user_id=7, window_id="@1", count=1
    )
    call = bot.sent[0]
    assert call["message_thread_id"] == 2
    assert call["disable_notification"] is True


async def test_voice_send_strips_card_actions_after_batch(monkeypatch) -> None:
    from ccgram_pro import state

    sc = state.WindowSidecar(window_id="@1", window_creation_epoch=0.0)
    sc.batch_mode = True
    state.save(sc)

    import ccgram.thread_router as tr

    monkeypatch.setattr(
        tr,
        "thread_router",
        SimpleNamespace(resolve_window_for_thread=lambda u, t: "@1"),
    )
    import ccgram.handlers.callback_helpers as ch

    monkeypatch.setattr(ch, "get_thread_id", lambda update: 2)

    async def _fake_enqueue(window_id: str, *, kind: str, body: str):
        return 3, None

    import ccgram_pro.input_pipeline.batcher as batcher

    monkeypatch.setattr(batcher, "enqueue", _fake_enqueue)

    bot = _Bot()
    stripped: dict[str, Any] = {}

    class _Query:
        data = "vc:send:99"

        async def edit_message_reply_markup(self, *, reply_markup: Any) -> None:
            stripped["markup"] = reply_markup

        async def answer(self, *a: Any, **k: Any) -> None:
            stripped["answered"] = (a, k)

    from ccgram.handlers.user_state import VOICE_PENDING

    msg = SimpleNamespace(chat=SimpleNamespace(id=10), get_bot=lambda: bot)
    ctx = SimpleNamespace(user_data={VOICE_PENDING: {(10, 99): "hello world"}})
    await intercept._wrapped_voice_send(msg, _Query(), 7, 99, object(), ctx)

    assert "markup" in stripped and stripped["markup"] is None
    assert stripped.get("answered") == (("Batched",), {})

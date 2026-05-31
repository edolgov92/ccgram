from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from ccgram.handlers.user_state import VOICE_PENDING
from ccgram_pro.input_pipeline import voice_edit
from telegram.ext import ApplicationHandlerStop


class _Query:
    def __init__(self, data: str, *, chat_id: int, msg_id: int, thread_id: int) -> None:
        self.data = data
        self.message = SimpleNamespace(
            message_id=msg_id,
            chat=SimpleNamespace(id=chat_id),
            message_thread_id=thread_id,
        )
        self.answers: list[Any] = []
        self.edits: list[dict[str, Any]] = []

    async def answer(self, *a: Any, **k: Any) -> None:
        self.answers.append((a, k))

    async def edit_message_text(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)


class _Bot:
    def __init__(self) -> None:
        self.edits: list[dict[str, Any]] = []

    async def edit_message_text(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)


def test_keyboard_has_add_edit_discard() -> None:
    markup = voice_edit.build_voice_keyboard(99)
    # All three actions live on a single row.
    assert len(markup.inline_keyboard) == 1
    flat = [b for row in markup.inline_keyboard for b in row]
    labels = [b.text for b in flat]
    cbs = [b.callback_data for b in flat]
    assert labels == ["➕ Add to batch", "✏️ Edit", "✗ Discard"]
    assert cbs == ["vc:send:99", "ccgrampro:ve:99", "vc:drop:99"]
    assert all(len(c.encode()) <= 64 for c in cbs)


async def test_edit_callback_arms_flag_and_prompts() -> None:
    ctx = SimpleNamespace(
        user_data={VOICE_PENDING: {(10, 99): "use the modal"}}, bot=_Bot()
    )
    query = _Query("ccgrampro:ve:99", chat_id=10, msg_id=500, thread_id=2)
    update = SimpleNamespace(callback_query=query)
    with pytest.raises(ApplicationHandlerStop):
        await voice_edit.handle_voice_edit_callback(update, ctx)
    pend = ctx.user_data[voice_edit.AWAITING_VOICE_EDIT]
    assert pend == {
        "chat_id": 10,
        "voice_msg_id": 99,
        "confirm_msg_id": 500,
        "thread_id": 2,
    }
    assert query.edits and "corrected" in query.edits[0]["text"].lower()


async def test_edit_callback_expired_pending_alerts() -> None:
    ctx = SimpleNamespace(user_data={VOICE_PENDING: {}}, bot=_Bot())
    query = _Query("ccgrampro:ve:99", chat_id=10, msg_id=500, thread_id=2)
    update = SimpleNamespace(callback_query=query)
    with pytest.raises(ApplicationHandlerStop):
        await voice_edit.handle_voice_edit_callback(update, ctx)
    assert voice_edit.AWAITING_VOICE_EDIT not in ctx.user_data
    assert query.answers and query.answers[-1][1].get("show_alert") is True


async def test_reply_replaces_pending_and_rerenders() -> None:
    bot = _Bot()
    ctx = SimpleNamespace(
        user_data={
            VOICE_PENDING: {(10, 99): "use the modal"},
            voice_edit.AWAITING_VOICE_EDIT: {
                "chat_id": 10,
                "voice_msg_id": 99,
                "confirm_msg_id": 500,
                "thread_id": 2,
            },
        },
        bot=bot,
    )
    deleted = {"v": False}

    async def _delete() -> None:
        deleted["v"] = True

    message = SimpleNamespace(
        text="use the model",
        message_thread_id=2,
        chat=SimpleNamespace(id=10),
        message_id=600,
        delete=_delete,
    )
    update = SimpleNamespace(message=message, callback_query=None)
    with pytest.raises(ApplicationHandlerStop):
        await voice_edit.consume_voice_edit_reply(update, ctx)
    assert ctx.user_data[VOICE_PENDING][(10, 99)] == "use the model"
    assert voice_edit.AWAITING_VOICE_EDIT not in ctx.user_data
    assert bot.edits and "use the model" in bot.edits[0]["text"]
    assert deleted["v"] is True


async def test_reply_wrong_thread_not_consumed() -> None:
    ctx = SimpleNamespace(
        user_data={
            VOICE_PENDING: {(10, 99): "orig"},
            voice_edit.AWAITING_VOICE_EDIT: {
                "chat_id": 10,
                "voice_msg_id": 99,
                "confirm_msg_id": 500,
                "thread_id": 2,
            },
        },
        bot=_Bot(),
    )
    message = SimpleNamespace(
        text="hi",
        message_thread_id=7,  # different topic
        chat=SimpleNamespace(id=10),
        message_id=600,
    )
    update = SimpleNamespace(message=message, callback_query=None)
    # Should NOT raise (pass-through), and pending stays unchanged.
    await voice_edit.consume_voice_edit_reply(update, ctx)
    assert ctx.user_data[VOICE_PENDING][(10, 99)] == "orig"
    assert voice_edit.AWAITING_VOICE_EDIT in ctx.user_data


async def test_reply_passthrough_when_not_armed() -> None:
    ctx = SimpleNamespace(user_data={}, bot=_Bot())
    message = SimpleNamespace(
        text="normal message",
        message_thread_id=2,
        chat=SimpleNamespace(id=10),
        message_id=600,
    )
    update = SimpleNamespace(message=message, callback_query=None)
    # No flag → returns without raising.
    await voice_edit.consume_voice_edit_reply(update, ctx)

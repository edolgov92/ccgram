from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from ccgram_pro.output_pipeline import interactive_clean, interactive_state
from ccgram_pro.output_pipeline.interactive_input import AskQuestion


@pytest.fixture(autouse=True)
def _reset():
    interactive_clean._reset_for_testing()
    yield
    interactive_clean._reset_for_testing()


class _Query:
    def __init__(self) -> None:
        self.answers: list[Any] = []
        self.edits: list[str] = []
        self.markup_edits: list[Any] = []

    async def answer(self, *a: Any, **k: Any) -> None:
        self.answers.append((a, k))

    async def edit_message_text(self, *, text: str, **k: Any) -> None:
        self.edits.append(text)

    async def edit_message_reply_markup(self, *, reply_markup: Any) -> None:
        self.markup_edits.append(reply_markup)


def _all_cbs(markup) -> list[str]:
    return [
        b.callback_data
        for row in markup.inline_keyboard
        for b in row
        if b.callback_data
    ]


def test_single_select_keyboard_one_button_per_option() -> None:
    markup = interactive_clean._question_keyboard(["Yes", "No"], False, set())
    cbs = _all_cbs(markup)
    assert cbs == ["ccgrampro:aq:p:0", "ccgrampro:aq:p:1"]
    assert all(len(c.encode()) <= 64 for c in cbs)


def test_multi_select_keyboard_has_confirm_cancel() -> None:
    markup = interactive_clean._question_keyboard(["A", "B"], True, {0})
    cbs = _all_cbs(markup)
    assert "ccgrampro:aq:t:0" in cbs and "ccgrampro:aq:t:1" in cbs
    assert "ccgrampro:aq:c" in cbs and "ccgrampro:aq:x" in cbs
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any(label.startswith("✅") for label in labels)  # selected idx 0 marked


def _seed_pending(window_id="@1", options=None, multi=False) -> None:
    interactive_clean._pending_asks[(7, 2)] = interactive_clean._PendingAsk(
        window_id=window_id,
        question="Which area?",
        options=options or ["Yes", "No"],
        multi_select=multi,
        chat_id=10,
        message_id=500,
    )


async def test_pick_drives_and_clears(monkeypatch) -> None:
    drove: list = []

    async def fake_drive(window_id, idx):
        drove.append((window_id, idx))
        return True

    monkeypatch.setattr(interactive_clean, "drive_single_select", fake_drive)
    _seed_pending()
    interactive_state.claim(7, 2)  # clean prompt owns the topic
    q = _Query()
    await interactive_clean._handle_aq_callback(q, 7, 2, "p:1")
    assert drove == [("@1", 1)]
    # Permanent Q&A record kept in history: question + chosen answer.
    assert "Which area?" in q.edits[0]
    assert "Your answer: No" in q.edits[0]
    assert (7, 2) not in interactive_clean._pending_asks
    assert interactive_state.is_owned(7, 2) is False  # ownership released on select


async def test_toggle_rerenders_keyboard() -> None:
    _seed_pending(multi=True, options=["A", "B"])
    q = _Query()
    await interactive_clean._handle_aq_callback(q, 7, 2, "t:0")
    assert interactive_clean._pending_asks[(7, 2)].selected == {0}
    assert q.markup_edits  # keyboard re-rendered with the checkmark


async def test_confirm_requires_selection() -> None:
    _seed_pending(multi=True, options=["A", "B"])
    q = _Query()
    await interactive_clean._handle_aq_callback(q, 7, 2, "c")
    assert q.answers and q.answers[-1][1].get("show_alert") is True
    assert (7, 2) in interactive_clean._pending_asks  # not cleared


async def test_stale_prompt_alerts() -> None:
    q = _Query()
    await interactive_clean._handle_aq_callback(q, 7, 2, "p:0")
    assert q.answers and q.answers[-1][1].get("show_alert") is True


async def test_notification_falls_through_for_other_tools(monkeypatch) -> None:
    called: list = []

    async def original(event, client):
        called.append(event)

    monkeypatch.setattr(interactive_clean, "_ORIGINAL_HANDLE_NOTIFICATION", original)
    event = SimpleNamespace(data={"tool_name": "PermissionRequest"})
    await interactive_clean._wrapped_handle_notification(event, object())
    assert called == [event]


def _wire_clean(monkeypatch, active) -> None:
    import ccgram.handlers.hook_events as he
    import ccgram.thread_router as tr

    monkeypatch.setattr(he, "_resolve_users_for_window_key", lambda key: [(7, 2, "@1")])
    monkeypatch.setattr(interactive_clean, "_resolve_transcript", lambda wid: "/x")
    monkeypatch.setattr(interactive_clean, "_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(interactive_clean, "_RETRY_DELAY", 0.0)
    monkeypatch.setattr(interactive_clean, "read_active_prompt", lambda path: active)
    # thread_router proxy is unwired in isolated tests — replace the module attr.
    monkeypatch.setattr(
        tr, "thread_router", SimpleNamespace(resolve_chat_id=lambda u, t: 10)
    )


async def test_maybe_post_clean_posts_question(monkeypatch) -> None:
    question = AskQuestion(
        tool_use_id="t1", question="Pick", options=["A", "B"], multi_select=False
    )
    _wire_clean(monkeypatch, ("ask", question))
    posts: list = []

    class _Client:
        async def send_message(self, **kwargs):
            posts.append(kwargs)
            return SimpleNamespace(message_id=999)

    event = SimpleNamespace(window_key="sess:@1", data={"tool_name": ""})
    handled = await interactive_clean._maybe_post_clean(event, _Client())
    assert handled is True
    assert posts and "Pick" in posts[0]["text"]
    assert (7, 2) in interactive_clean._pending_asks
    assert interactive_state.is_owned(7, 2) is True  # owns the topic → scraped UI off


async def test_maybe_post_clean_releases_when_no_active_prompt(monkeypatch) -> None:
    _wire_clean(monkeypatch, None)  # no live AUQ/EPM → not ours

    class _Client:
        async def send_message(self, **kwargs):
            raise AssertionError("must not post when no active prompt")

    event = SimpleNamespace(window_key="sess:@1", data={"tool_name": ""})
    handled = await interactive_clean._maybe_post_clean(event, _Client())
    assert handled is False
    assert interactive_state.is_owned(7, 2) is False  # released → scraped UI fallback


async def test_ensure_clean_prompt_posts_and_owns(monkeypatch) -> None:
    question = AskQuestion(
        tool_use_id="t1", question="Pick", options=["A", "B"], multi_select=False
    )
    _wire_clean(monkeypatch, ("ask", question))
    posts: list = []

    class _Client:
        async def send_message(self, **kwargs):
            posts.append(kwargs)
            return SimpleNamespace(message_id=999)

    ok = await interactive_clean.ensure_clean_prompt(
        _Client(), user_id=7, thread_id=2, window_id="@1"
    )
    assert ok is True
    assert posts and "Pick" in posts[0]["text"]
    assert interactive_state.is_owned(7, 2) is True


async def test_ensure_clean_prompt_idempotent_when_owned() -> None:
    interactive_state.claim(7, 2)

    class _Client:
        async def send_message(self, **kwargs):
            raise AssertionError("must not post when already owned")

    ok = await interactive_clean.ensure_clean_prompt(
        _Client(), user_id=7, thread_id=2, window_id="@1"
    )
    assert ok is True


async def test_ensure_clean_prompt_false_for_non_clean(monkeypatch) -> None:
    _wire_clean(monkeypatch, None)  # permission / non-clean prompt

    class _Client:
        async def send_message(self, **kwargs):
            raise AssertionError("must not post for a non-clean prompt")

    ok = await interactive_clean.ensure_clean_prompt(
        _Client(), user_id=7, thread_id=2, window_id="@1"
    )
    assert ok is False
    assert interactive_state.is_owned(7, 2) is False  # released → scraped fallback


async def test_maybe_post_clean_skips_owned_binding(monkeypatch) -> None:
    question = AskQuestion(
        tool_use_id="t1", question="Pick", options=["A", "B"], multi_select=False
    )
    _wire_clean(monkeypatch, ("ask", question))
    interactive_state.claim(7, 2)  # the poll-guard already handled this binding

    class _Client:
        async def send_message(self, **kwargs):
            raise AssertionError("must not double-post an already-owned binding")

    event = SimpleNamespace(window_key="sess:@1", data={"tool_name": ""})
    handled = await interactive_clean._maybe_post_clean(event, _Client())
    assert handled is True  # treated as handled (the guard posted it)

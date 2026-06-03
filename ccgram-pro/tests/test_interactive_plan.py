from __future__ import annotations

from typing import Any

import pytest
from ccgram_pro.output_pipeline import interactive_plan


@pytest.fixture(autouse=True)
def _reset():
    interactive_plan._reset_for_testing()
    yield
    interactive_plan._reset_for_testing()


class _Query:
    def __init__(self) -> None:
        self.answers: list[Any] = []
        self.edits: list[str] = []

    async def answer(self, *a: Any, **k: Any) -> None:
        self.answers.append((a, k))

    async def edit_message_text(self, *, text: str, **k: Any) -> None:
        self.edits.append(text)


def test_plan_keyboard_has_approve_keep_view_settings() -> None:
    markup = interactive_plan._plan_keyboard(
        plan_url="https://x/plan/t", window_id="@1"
    )
    flat = [b for row in markup.inline_keyboard for b in row]
    cbs = [b.callback_data for b in flat if b.callback_data]
    urls = [b.url for b in flat if b.url]
    assert "ccgrampro:pl:a" in cbs
    assert "ccgrampro:pl:k" in cbs
    assert "https://x/plan/t" in urls
    assert any(c.startswith("ccgrampro:set:open:@1") for c in cbs)
    assert all(len(c.encode()) <= 64 for c in cbs)


def test_plan_keyboard_omits_view_without_url() -> None:
    markup = interactive_plan._plan_keyboard(plan_url=None, window_id="@1")
    urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
    assert urls == []


async def test_approve_drives_index_0(monkeypatch) -> None:
    drove: list = []

    async def fake_single(window_id, idx):
        drove.append((window_id, idx))
        return True

    monkeypatch.setattr(interactive_plan, "drive_single_select", fake_single)
    interactive_plan._pending_plans[(7, 2)] = ("@1", "Build the thing")
    q = _Query()
    await interactive_plan.handle_plan_callback(q, 7, 2, "ccgrampro:pl:a")
    assert drove == [("@1", 0)]
    assert "approved" in q.edits[0].lower()
    assert (7, 2) not in interactive_plan._pending_plans


async def test_keep_planning_sends_escape(monkeypatch) -> None:
    cancelled: list = []

    async def fake_cancel(window_id):
        cancelled.append(window_id)
        return True

    monkeypatch.setattr(interactive_plan, "drive_cancel", fake_cancel)
    interactive_plan._pending_plans[(7, 2)] = ("@1", "Build the thing")
    q = _Query()
    await interactive_plan.handle_plan_callback(q, 7, 2, "ccgrampro:pl:k")
    assert cancelled == ["@1"]
    assert "planning" in q.edits[0].lower()


async def test_plan_callback_stale_alerts() -> None:
    q = _Query()
    await interactive_plan.handle_plan_callback(q, 7, 2, "ccgrampro:pl:a")
    assert q.answers and q.answers[-1][1].get("show_alert") is True


async def test_post_plan_fast_skips_llm_condense(monkeypatch) -> None:
    from types import SimpleNamespace

    # If fast=True ever calls the (slow) LLM condense, fail loudly.
    async def _boom(_md):
        raise AssertionError("fast path must not call the LLM condense")

    monkeypatch.setattr(interactive_plan, "condense_plan", _boom)
    monkeypatch.setattr(interactive_plan, "save_share", lambda **k: "sid")
    monkeypatch.setattr(interactive_plan, "make_plan_url", lambda **k: None)
    import ccgram.thread_router as tr

    monkeypatch.setattr(
        tr, "thread_router", SimpleNamespace(resolve_chat_id=lambda u, t: 10)
    )
    posts: list = []

    class _Client:
        async def send_message(self, **kwargs):
            posts.append(kwargs)
            return SimpleNamespace(message_id=1)

    plan_md = "# Build the widget\n\nWe add a widget to the dashboard."
    keys = await interactive_plan.post_plan(
        _Client(), [(7, 2, "@1")], plan_md, fast=True
    )
    assert keys == {(7, 2)}
    assert posts and "Build the widget" in posts[0]["text"]

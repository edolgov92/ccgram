from __future__ import annotations

import pytest
from ccgram_pro.output_pipeline import interactive_state


@pytest.fixture(autouse=True)
def _reset():
    interactive_state._reset_for_testing()
    yield
    interactive_state._reset_for_testing()


def test_claim_release_is_owned() -> None:
    assert interactive_state.is_owned(7, 2) is False
    interactive_state.claim(7, 2)
    assert interactive_state.is_owned(7, 2) is True
    interactive_state.release(7, 2)
    assert interactive_state.is_owned(7, 2) is False


async def test_guard_suppresses_when_owned() -> None:
    calls: list = []

    async def original(client, user_id, window_id, thread_id=None, *a, **k):
        calls.append((user_id, thread_id))
        return False

    wrapped = interactive_state._wrap_handle_interactive_ui(original)
    interactive_state.claim(7, 2)
    result = await wrapped("client", 7, "@1", 2)
    assert result is True  # reported handled (scraped UI suppressed)
    assert calls == []  # original never ran


async def test_guard_passes_through_when_not_owned() -> None:
    calls: list = []

    async def original(client, user_id, window_id, thread_id=None, *a, **k):
        calls.append((user_id, thread_id))
        return False

    wrapped = interactive_state._wrap_handle_interactive_ui(original)
    result = await wrapped("client", 7, "@1", 2)
    assert result is False
    assert calls == [(7, 2)]  # original ran (no clean prompt owns the topic)


async def test_guard_triggers_clean_ui_first(monkeypatch) -> None:
    # When not owned, the guard tries the clean UI FIRST; if it handles the
    # prompt the scraped UI is suppressed (original never runs).
    calls: list = []

    async def original(client, user_id, window_id, thread_id=None, *a, **k):
        calls.append((user_id, thread_id))
        return False

    captured: dict = {}

    async def _ensure(client, *, user_id, thread_id, window_id):
        captured.update(user_id=user_id, thread_id=thread_id, window_id=window_id)
        return True

    import ccgram_pro.output_pipeline.interactive_clean as ic

    monkeypatch.setattr(ic, "ensure_clean_prompt", _ensure)
    wrapped = interactive_state._wrap_handle_interactive_ui(original)
    result = await wrapped("client", 7, "@1", 2)
    assert result is True
    assert calls == []  # scraped UI suppressed
    assert captured == {"user_id": 7, "thread_id": 2, "window_id": "@1"}


async def test_guard_falls_back_when_not_a_clean_prompt(monkeypatch) -> None:
    calls: list = []

    async def original(client, user_id, window_id, thread_id=None, *a, **k):
        calls.append((user_id, thread_id))
        return False

    async def _ensure(client, *, user_id, thread_id, window_id):
        return False  # permission / non-clean prompt

    import ccgram_pro.output_pipeline.interactive_clean as ic

    monkeypatch.setattr(ic, "ensure_clean_prompt", _ensure)
    wrapped = interactive_state._wrap_handle_interactive_ui(original)
    await wrapped("client", 7, "@1", 2)
    assert calls == [(7, 2)]  # scraped fallback ran


async def test_guard_falls_back_when_clean_errors(monkeypatch) -> None:
    calls: list = []

    async def original(client, user_id, window_id, thread_id=None, *a, **k):
        calls.append((user_id, thread_id))
        return False

    async def _ensure(client, *, user_id, thread_id, window_id):
        raise RuntimeError("boom")

    import ccgram_pro.output_pipeline.interactive_clean as ic

    monkeypatch.setattr(ic, "ensure_clean_prompt", _ensure)
    wrapped = interactive_state._wrap_handle_interactive_ui(original)
    await wrapped("client", 7, "@1", 2)
    assert calls == [(7, 2)]  # error in clean path → still falls back to scraped


async def test_guard_passes_through_when_thread_none() -> None:
    calls: list = []

    async def original(client, user_id, window_id, thread_id=None, *a, **k):
        calls.append(user_id)
        return True

    wrapped = interactive_state._wrap_handle_interactive_ui(original)
    interactive_state.claim(7, 0)  # claim under thread 0
    # A call with thread_id=None must NOT be suppressed by a thread-0 claim.
    await wrapped("client", 7, "@1", None)
    assert calls == [7]

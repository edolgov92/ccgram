from __future__ import annotations

import pytest
from ccgram_pro import new_session_store as store


@pytest.fixture(autouse=True)
def _reset():
    store._reset_for_testing()
    yield
    store._reset_for_testing()


def test_create_and_get() -> None:
    s = store.create(10, 20, 99, "hi", default_mode="plan")
    assert s.mode == "plan"
    got = store.get(10, 20)
    assert got is s
    assert got.first_text == "hi"


def test_get_returns_none_when_absent() -> None:
    assert store.get(1, 2) is None


def test_threads_are_isolated() -> None:
    a = store.create(100, 1, 7, "a", default_mode="coding")
    b = store.create(100, 2, 7, "b", default_mode="coding")
    a.model_key = "opus48-1m"
    assert store.get(100, 2) is b
    assert b.model_key == "opus48"


def test_append_text_and_combined() -> None:
    store.create(5, 6, 7, "first", default_mode="coding")
    store.append_text(5, 6, "second")
    store.append_text(5, 6, "third")
    s = store.get(5, 6)
    assert s is not None
    assert s.combined_text() == "first\n\nsecond\n\nthird"


def test_clear() -> None:
    store.create(1, 1, 1, "x", default_mode="coding")
    store.clear(1, 1)
    assert store.get(1, 1) is None


def test_lazy_expiry(monkeypatch) -> None:
    s = store.create(1, 1, 1, "x", default_mode="coding")
    s.created_at -= store._PICKER_TTL_SECONDS + 1
    assert store.get(1, 1) is None

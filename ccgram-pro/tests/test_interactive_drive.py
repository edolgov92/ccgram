from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from ccgram_pro.output_pipeline import interactive_drive


class _Tmux:
    def __init__(self, *, window_found: bool = True) -> None:
        self.window_found = window_found
        self.keys: list[str] = []

    async def find_window_by_id(self, window_id: str) -> Any:
        return SimpleNamespace(window_id=window_id) if self.window_found else None

    async def send_keys(
        self, window_id: str, key: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self.keys.append(key)
        return True


@pytest.fixture(autouse=True)
def _fast_keys(monkeypatch):
    monkeypatch.setattr(interactive_drive, "_KEY_DELAY", 0.0)


def _install(monkeypatch, tmux: _Tmux) -> None:
    import ccgram.tmux_manager as tm

    monkeypatch.setattr(tm, "tmux_manager", tmux)


async def test_single_select_index_2(monkeypatch) -> None:
    tmux = _Tmux()
    _install(monkeypatch, tmux)
    ok = await interactive_drive.drive_single_select("@1", 2)
    assert ok is True
    reset = ["Up"] * interactive_drive._RESET_PRESSES
    assert tmux.keys == reset + ["Down", "Down", "Enter"]


async def test_single_select_index_0(monkeypatch) -> None:
    tmux = _Tmux()
    _install(monkeypatch, tmux)
    await interactive_drive.drive_single_select("@1", 0)
    reset = ["Up"] * interactive_drive._RESET_PRESSES
    assert tmux.keys == reset + ["Enter"]


async def test_multi_select_toggles_then_enter(monkeypatch) -> None:
    tmux = _Tmux()
    _install(monkeypatch, tmux)
    await interactive_drive.drive_multi_select("@1", [1, 3])
    reset = ["Up"] * interactive_drive._RESET_PRESSES
    assert tmux.keys == reset + ["Down", "Space", "Down", "Down", "Space", "Enter"]


async def test_cancel_sends_escape(monkeypatch) -> None:
    tmux = _Tmux()
    _install(monkeypatch, tmux)
    await interactive_drive.drive_cancel("@1")
    assert tmux.keys == ["Escape"]


async def test_returns_false_when_window_missing(monkeypatch) -> None:
    tmux = _Tmux(window_found=False)
    _install(monkeypatch, tmux)
    assert await interactive_drive.drive_single_select("@gone", 0) is False
    assert await interactive_drive.drive_cancel("@gone") is False
    assert tmux.keys == []


async def test_multi_select_empty_returns_false(monkeypatch) -> None:
    tmux = _Tmux()
    _install(monkeypatch, tmux)
    assert await interactive_drive.drive_multi_select("@1", []) is False

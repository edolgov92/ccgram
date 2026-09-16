from __future__ import annotations

import pytest
from ccgram_pro.output_pipeline import tui_dialogs

_RESUME = """
  This session is 1d 2h old and 316.3k tokens.

  Resuming the full session will consume a substantial portion of your usage
  limits. We recommend resuming from a summary.

  ❯ 1. Resume from summary (recommended)
    2. Resume full session as-is
"""

_BUG_REPORT = """
╭──────────────────────────────────────────────────────────────╮
│ ✻ Bug report drafted: Scripted string-replace edit silently… │
│ │ - What happened: I added a call to connectWebAnalytics()   │
│ 1 to review · 2 to send · 0 to dismiss                       │
╰──────────────────────────────────────────────────────────────╯
❯
"""

_FEEDBACK_OPTOUT = """
  ⎿  Set model to Opus 5 (1M context)
Turn off Claude-drafted feedback? 0 to turn off · Esc to keep
❯
"""

_IDLE = "❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"


@pytest.fixture(autouse=True)
def _reset():
    tui_dialogs._reset_for_testing()
    yield
    tui_dialogs._reset_for_testing()


def test_matches_each_known_dialog() -> None:
    assert tui_dialogs.match_dialog(_RESUME).name == "resume_summary"
    assert tui_dialogs.match_dialog(_BUG_REPORT).name == "bug_report"
    assert tui_dialogs.match_dialog(_FEEDBACK_OPTOUT).name == "feedback_optout"


@pytest.mark.parametrize(
    "text",
    [
        "",
        _IDLE,
        "We recommend resuming from a summary.",
        "  2. Resume full session as-is",
        "Bug report drafted",
        "Turn off Claude-drafted feedback?",
    ],
)
def test_ignores_fragments_and_idle_screens(text: str) -> None:
    assert tui_dialogs.is_blocking_dialog(text) is False


def _stub(monkeypatch, pane: str):
    import ccgram.tmux_manager as tm
    from ccgram_pro.output_pipeline import interactive_drive

    selected: list[tuple[str, int]] = []
    pressed: list[tuple[str, list[str]]] = []

    async def _capture(window_id, with_ansi=False):  # noqa: ANN001
        return pane

    async def _select(window_id, index):  # noqa: ANN001
        selected.append((window_id, index))
        return True

    async def _press(window_id, keys):  # noqa: ANN001
        pressed.append((window_id, list(keys)))
        return True

    monkeypatch.setattr(tm.tmux_manager, "capture_pane", _capture)
    monkeypatch.setattr(interactive_drive, "drive_single_select", _select)
    monkeypatch.setattr(interactive_drive, "press_keys", _press)
    return selected, pressed


async def test_resume_dialog_picks_full_session(monkeypatch) -> None:
    selected, pressed = _stub(monkeypatch, _RESUME)
    assert await tui_dialogs.auto_answer("@5") is True
    assert selected == [("@5", tui_dialogs.FULL_SESSION_INDEX)]
    assert tui_dialogs.FULL_SESSION_INDEX == 1
    assert pressed == []


async def test_bug_report_is_dismissed_never_sent(monkeypatch) -> None:
    selected, pressed = _stub(monkeypatch, _BUG_REPORT)
    assert await tui_dialogs.auto_answer("@5") is True
    assert pressed == [("@5", ["0"])]  # "0" dismiss, never "2" (send)
    assert selected == []


async def test_feedback_optout_keeps_the_setting(monkeypatch) -> None:
    _selected, pressed = _stub(monkeypatch, _FEEDBACK_OPTOUT)
    assert await tui_dialogs.auto_answer("@5") is True
    assert pressed == [("@5", ["Escape"])]  # Esc keeps, never "0" (turn off)


async def test_idle_screen_passes_through(monkeypatch) -> None:
    selected, pressed = _stub(monkeypatch, _IDLE)
    assert await tui_dialogs.auto_answer("@5") is False
    assert selected == [] and pressed == []


async def test_cooldown_prevents_double_drive(monkeypatch) -> None:
    _selected, pressed = _stub(monkeypatch, _BUG_REPORT)
    assert await tui_dialogs.auto_answer("@5") is True
    assert await tui_dialogs.auto_answer("@5") is True
    assert pressed == [("@5", ["0"])]


async def test_guard_answers_dialog_before_scraped_ui(monkeypatch) -> None:
    from ccgram_pro.output_pipeline import interactive_state
    from ccgram_pro.output_pipeline import tui_dialogs as td

    async def _answer(window_id):  # noqa: ANN001
        return True

    monkeypatch.setattr(td, "auto_answer", _answer)
    monkeypatch.setattr(interactive_state, "is_owned", lambda u, t: False)
    called: list[int] = []

    async def _original(*a, **k):  # noqa: ANN001
        called.append(1)
        return False

    wrapped = interactive_state._wrap_handle_interactive_ui(_original)
    assert await wrapped(object(), 7, "@5", 2) is True
    assert called == []

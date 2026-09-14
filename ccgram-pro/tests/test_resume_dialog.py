from __future__ import annotations

import pytest
from ccgram_pro.output_pipeline import resume_dialog

_DIALOG = """
  This session is 1d 2h old and 316.3k tokens.

  Resuming the full session will consume a substantial portion of your usage
  limits. We recommend resuming from a summary.

  ❯ 1. Resume from summary (recommended)
    2. Resume full session as-is
"""


@pytest.fixture(autouse=True)
def _reset():
    resume_dialog._reset_for_testing()
    yield
    resume_dialog._reset_for_testing()


def test_detects_dialog() -> None:
    assert resume_dialog.is_stale_resume_dialog(_DIALOG) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "❯ \n  ⏵⏵ bypass permissions on",
        "We recommend resuming from a summary.",
        "  2. Resume full session as-is",
    ],
)
def test_ignores_non_dialog_screens(text: str) -> None:
    assert resume_dialog.is_stale_resume_dialog(text) is False


async def test_auto_answer_picks_full_session(monkeypatch) -> None:
    import ccgram.tmux_manager as tm
    from ccgram_pro.output_pipeline import interactive_drive

    async def _capture(window_id, with_ansi=False):  # noqa: ANN001
        return _DIALOG

    picked: list[tuple[str, int]] = []

    async def _drive(window_id, index):  # noqa: ANN001
        picked.append((window_id, index))
        return True

    monkeypatch.setattr(tm.tmux_manager, "capture_pane", _capture)
    monkeypatch.setattr(interactive_drive, "drive_single_select", _drive)
    assert await resume_dialog.auto_answer("@5") is True
    assert picked == [("@5", resume_dialog.FULL_SESSION_INDEX)]
    assert resume_dialog.FULL_SESSION_INDEX == 1


async def test_auto_answer_no_dialog_passes_through(monkeypatch) -> None:
    import ccgram.tmux_manager as tm
    from ccgram_pro.output_pipeline import interactive_drive

    async def _capture(window_id, with_ansi=False):  # noqa: ANN001
        return "❯ \n  ⏵⏵ bypass permissions on"

    picked: list[tuple[str, int]] = []

    async def _drive(window_id, index):  # noqa: ANN001
        picked.append((window_id, index))
        return True

    monkeypatch.setattr(tm.tmux_manager, "capture_pane", _capture)
    monkeypatch.setattr(interactive_drive, "drive_single_select", _drive)
    assert await resume_dialog.auto_answer("@5") is False
    assert picked == []


async def test_auto_answer_cooldown_prevents_double_drive(monkeypatch) -> None:
    import ccgram.tmux_manager as tm
    from ccgram_pro.output_pipeline import interactive_drive

    async def _capture(window_id, with_ansi=False):  # noqa: ANN001
        return _DIALOG

    picked: list[int] = []

    async def _drive(window_id, index):  # noqa: ANN001
        picked.append(index)
        return True

    monkeypatch.setattr(tm.tmux_manager, "capture_pane", _capture)
    monkeypatch.setattr(interactive_drive, "drive_single_select", _drive)
    assert await resume_dialog.auto_answer("@5") is True
    assert await resume_dialog.auto_answer("@5") is True
    assert picked == [1]


async def test_guard_answers_dialog_before_scraped_ui(monkeypatch) -> None:
    from ccgram_pro.output_pipeline import interactive_state, resume_dialog as rd

    async def _answer(window_id):  # noqa: ANN001
        return True

    monkeypatch.setattr(rd, "auto_answer", _answer)
    monkeypatch.setattr(interactive_state, "is_owned", lambda u, t: False)
    called: list[int] = []

    async def _original(*a, **k):  # noqa: ANN001
        called.append(1)
        return False

    wrapped = interactive_state._wrap_handle_interactive_ui(_original)
    assert await wrapped(object(), 7, "@5", 2) is True
    assert called == []

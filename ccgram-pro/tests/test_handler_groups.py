from __future__ import annotations

from typing import Any

from telegram.ext import MessageHandler


class _RecordingApp:
    def __init__(self) -> None:
        self.handlers: list[tuple[int, Any]] = []

    def add_handler(self, handler: Any, group: int = 0) -> None:
        self.handlers.append((group, handler))


def _snapshot_intercept_patches(monkeypatch) -> None:
    # install_input_pipeline reassigns these ccgram module globals to wrapped
    # versions; snapshot them so the wrapping never leaks into the rest of the
    # suite (monkeypatch restores the originals at teardown).
    from ccgram.handlers.text import text_handler as th
    from ccgram.handlers.voice import voice_callbacks as vc
    from ccgram.handlers.voice import voice_handler as vh

    for mod, name in (
        (th, "_forward_message"),
        (vc, "_handle_send"),
        (vh, "_transcribe_audio"),
        (vh, "_build_voice_keyboard"),
    ):
        monkeypatch.setattr(mod, name, getattr(mod, name))


def test_layer_text_consumers_use_distinct_groups(monkeypatch) -> None:
    from ccgram_pro import git_composer, scenarios
    from ccgram_pro.input_pipeline import intercept, voice_edit

    _snapshot_intercept_patches(monkeypatch)
    intercept._reset_for_testing()
    scenarios._reset_for_testing()
    git_composer._reset_for_testing()

    app = _RecordingApp()
    intercept.install_input_pipeline(app)
    scenarios.install_scenarios(app)
    git_composer.install_git_composer(app)

    # The free-text (TEXT-filter) consumers that all match any text message — the
    # ones that would shadow each other if they shared a PTB group. (Photo/doc
    # handlers use disjoint filters and may legitimately share a group.)
    targets = {
        voice_edit.consume_voice_edit_reply,
        scenarios.consume_pr_number_reply,
        git_composer.capture_composer_text,
    }
    groups = [g for g, h in app.handlers if getattr(h, "callback", None) in targets]
    assert len(groups) == 3, "expected all three free-text consumers registered"
    # Distinct groups — PTB runs at most one handler per group, so a shared group
    # would silently shadow all but the first-registered consumer.
    assert len(set(groups)) == 3, (
        f"free-text consumers share a group (one would be shadowed): {sorted(groups)}"
    )
    # All ahead of ccgram's core text handler (group 0).
    assert all(g < 0 for g in groups)

    intercept._reset_for_testing()
    scenarios._reset_for_testing()
    git_composer._reset_for_testing()


def test_git_composer_reply_has_its_own_group() -> None:
    from ccgram_pro import git_composer

    git_composer._reset_for_testing()
    app = _RecordingApp()
    git_composer.install_git_composer(app)
    msg_groups = [g for g, h in app.handlers if isinstance(h, MessageHandler)]
    assert msg_groups == [-13]
    git_composer._reset_for_testing()

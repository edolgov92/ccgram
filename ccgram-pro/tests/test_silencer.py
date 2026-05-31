from __future__ import annotations

from types import SimpleNamespace

from ccgram_pro import state
from ccgram_pro.output_pipeline import silencer


def _msg(**kw):
    return SimpleNamespace(**kw)


def test_interactive_tool_not_silenced() -> None:
    from ccgram.handlers.interactive import INTERACTIVE_TOOL_NAMES

    tool = next(iter(INTERACTIVE_TOOL_NAMES))
    msg = _msg(session_id="s1", tool_name=tool, content_type="tool_use")
    assert silencer._handle_new_message_silent(msg) is False


def test_non_interactive_silenced_when_session_silent(monkeypatch) -> None:
    monkeypatch.setattr(silencer, "_is_silent_for_session", lambda sid: True)
    msg = _msg(session_id="s1", tool_name="", content_type="text")
    assert silencer._handle_new_message_silent(msg) is True


def test_message_without_session_not_silenced() -> None:
    msg = _msg(session_id="", tool_name="", content_type="text")
    assert silencer._handle_new_message_silent(msg) is False


def test_is_silent_for_window_reads_sidecar() -> None:
    state.save(
        state.WindowSidecar(
            window_id="@s", window_creation_epoch=0.0, silent_mode=False
        )
    )
    assert silencer._is_silent_for_window("@s") is False
    state.save(
        state.WindowSidecar(window_id="@t", window_creation_epoch=0.0, silent_mode=True)
    )
    assert silencer._is_silent_for_window("@t") is True


def test_is_silent_for_window_none_id() -> None:
    assert silencer._is_silent_for_window(None) is False


def test_already_wrapped_guard() -> None:
    async def original() -> None: ...

    wrapped = silencer._wrap_async("x", original, lambda *a, **k: False)
    assert silencer._already_wrapped(wrapped) is True
    assert silencer._already_wrapped(original) is False


def _enable_reactions() -> None:
    from ccgram_pro.config import ensure_layer_dirs, settings_toml_path

    ensure_layer_dirs()
    settings_toml_path().write_text("[defaults]\nreactions_enabled = true\n")


def test_reactions_disabled_by_default() -> None:
    assert silencer._reactions_disabled() is True


def test_reactions_enabled_when_configured() -> None:
    _enable_reactions()
    assert silencer._reactions_disabled() is False


async def test_wrapped_ack_reaction_noops_when_disabled() -> None:
    calls: list = []

    async def original(*a, **k) -> None:
        calls.append((a, k))

    wrapped = silencer._wrap_async(
        "ack_reaction", original, silencer._reactions_disabled
    )
    result = await wrapped("client", 10, 99)
    assert result is None
    assert calls == []


async def test_wrapped_ack_reaction_runs_when_enabled() -> None:
    _enable_reactions()
    calls: list = []

    async def original(*a, **k) -> None:
        calls.append((a, k))

    wrapped = silencer._wrap_async(
        "ack_reaction", original, silencer._reactions_disabled
    )
    await wrapped("client", 10, 99)
    assert len(calls) == 1


async def test_wrap_react_returns_true_when_disabled() -> None:
    calls: list = []

    async def original(*a, **k) -> bool:
        calls.append((a, k))
        return False

    wrapped = silencer._wrap_react("react", original)
    result = await wrapped("client", 10, 99, "👀")
    assert result is True
    assert calls == []


async def test_wrap_react_calls_original_when_enabled() -> None:
    _enable_reactions()
    calls: list = []

    async def original(*a, **k) -> bool:
        calls.append((a, k))
        return True

    wrapped = silencer._wrap_react("react", original)
    result = await wrapped("client", 10, 99, "👀")
    assert result is True
    assert len(calls) == 1

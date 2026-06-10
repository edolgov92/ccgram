from __future__ import annotations

from types import SimpleNamespace

import pytest
from ccgram_pro import settings_panel as sp
from ccgram_pro import state


@pytest.fixture(autouse=True)
def _reset():
    sp._reset_for_testing()
    yield
    sp._reset_for_testing()


def test_codec_roundtrip_managed_window() -> None:
    data = sp.encode("m", "fable5-1m", "@5")
    assert sp.decode(data) == ("m", "fable5-1m", "@5")


def test_codec_roundtrip_foreign_window() -> None:
    data = sp.encode("e", "xhigh", "sess:@9")
    assert sp.decode(data) == ("e", "xhigh", "sess:@9")


def test_codec_open_and_close_have_no_payload() -> None:
    assert sp.decode(sp.encode("open", None, "@1")) == ("open", None, "@1")
    assert sp.decode(sp.encode("x", None, "sess:@9")) == ("x", None, "sess:@9")


def test_codec_within_64_bytes_worst_case() -> None:
    data = sp.encode("m", "fable5-1m", "averylongforeignsession:@99")
    assert len(data) <= 64


def test_codec_rejects_garbage() -> None:
    assert sp.decode("not-ours") is None
    assert sp.decode("ccgrampro:set:m") is None


def test_button_for_window_under_64() -> None:
    btn = sp.button_for_window("sess:@9")
    assert len(btn.callback_data) <= 64
    assert btn.callback_data.startswith("ccgrampro:set:open:")


def test_build_keyboard_marks_and_callbacks() -> None:
    sidecar = state.WindowSidecar(
        window_id="@5",
        window_creation_epoch=0.0,
        model="fable5-1m",
        reasoning="max",
        mode="plan",
    )
    kb = sp.build_settings_keyboard("@5", sidecar)
    flat_text = [b.text for row in kb.inline_keyboard for b in row]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(t.startswith("● ") and "1M" in t for t in flat_text)
    assert any(t == "● Max" for t in flat_text)
    assert any(t == "● Plan" for t in flat_text)
    assert "ccgrampro:set:m:fable5:@5" in datas
    assert "ccgrampro:set:e:low:@5" in datas
    assert "ccgrampro:git:menu" in datas


def test_build_keyboard_maps_legacy_values() -> None:
    sidecar = state.WindowSidecar(
        window_id="@5", window_creation_epoch=0.0, model="opus", reasoning="extra-high"
    )
    kb = sp.build_settings_keyboard("@5", sidecar)
    flat_text = [b.text for row in kb.inline_keyboard for b in row]
    assert any(t == "● Fable 5" for t in flat_text)
    assert any(t == "● X-High" for t in flat_text)


async def test_apply_model_sends_slash_model(monkeypatch) -> None:
    sent: list[str] = []

    async def stub(window_id, text):  # noqa: ARG001
        sent.append(text)
        return True, ""

    import ccgram.tmux_manager as tm

    monkeypatch.setattr(tm, "send_to_window", stub)
    assert await sp.apply_model("@5", "fable5-1m") is True
    assert sent == ["/model claude-fable-5[1m]"]


async def test_apply_effort_sends_slash_effort(monkeypatch) -> None:
    sent: list[str] = []

    async def stub(window_id, text):  # noqa: ARG001
        sent.append(text)
        return True, ""

    import ccgram.tmux_manager as tm

    monkeypatch.setattr(tm, "send_to_window", stub)
    assert await sp.apply_effort("@5", "xhigh") is True
    assert sent == ["/effort xhigh"]


async def test_apply_mode_delegates_to_drive(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def stub(window_id, target):
        calls.append((window_id, target))
        return True

    import ccgram_pro.plan_mode as pm

    monkeypatch.setattr(pm, "drive_to_mode", stub)
    assert await sp.apply_mode("@5", "plan") is True
    assert calls == [("@5", "plan")]
    await sp.apply_mode("@5", "code")
    assert calls[-1] == ("@5", "coding")


class _Query:
    def __init__(self) -> None:
        self.answers: list[tuple[tuple, dict]] = []
        self.edits: list[dict] = []
        self.message = None

    async def answer(self, *a, **k) -> None:
        self.answers.append((a, k))

    async def edit_message_text(self, **k) -> None:
        self.edits.append(k)


def _stub_router(monkeypatch) -> None:
    import ccgram.thread_router as tr

    monkeypatch.setattr(
        tr, "thread_router", SimpleNamespace(get_display_name=lambda wid: wid)
    )


async def test_mode_applies_mid_session(monkeypatch) -> None:
    state.save(state.WindowSidecar(window_id="@5", window_creation_epoch=0.0))
    applied: list[tuple[str, str]] = []

    async def _apply_mode(window_id, payload):
        applied.append((window_id, payload))
        return True

    _stub_router(monkeypatch)
    monkeypatch.setattr(sp, "apply_mode", _apply_mode)
    query = _Query()
    await sp._apply_change(query, "@5", "mo", "plan")
    assert applied == [("@5", "plan")]
    assert any("Plan" in str(a) for a, _k in query.answers)
    assert not any("busy" in str(a).lower() for a, _k in query.answers)
    assert state.load("@5").mode == "plan"


async def test_model_applies_mid_session(monkeypatch) -> None:
    state.save(state.WindowSidecar(window_id="@5", window_creation_epoch=0.0))
    applied: list[str] = []

    async def _apply_model(window_id, payload):  # noqa: ARG001
        applied.append(payload)
        return True

    _stub_router(monkeypatch)
    monkeypatch.setattr(sp, "apply_model", _apply_model)
    query = _Query()
    await sp._apply_change(query, "@5", "m", "fable5-1m")
    assert applied == ["fable5-1m"]
    assert not any("busy" in str(a).lower() for a, _k in query.answers)
    assert state.load("@5").model == "fable5-1m"


async def test_effort_applies_mid_session(monkeypatch) -> None:
    state.save(state.WindowSidecar(window_id="@5", window_creation_epoch=0.0))
    applied: list[str] = []

    async def _apply_effort(window_id, payload):  # noqa: ARG001
        applied.append(payload)
        return True

    _stub_router(monkeypatch)
    monkeypatch.setattr(sp, "apply_effort", _apply_effort)
    query = _Query()
    await sp._apply_change(query, "@5", "e", "max")
    assert applied == ["max"]
    assert not any("busy" in str(a).lower() for a, _k in query.answers)
    assert state.load("@5").reasoning == "max"


async def test_failed_apply_reports_error(monkeypatch) -> None:
    state.save(state.WindowSidecar(window_id="@5", window_creation_epoch=0.0))

    async def _apply_effort(window_id, payload):  # noqa: ARG001
        return False

    _stub_router(monkeypatch)
    monkeypatch.setattr(sp, "apply_effort", _apply_effort)
    query = _Query()
    await sp._apply_change(query, "@5", "e", "max")
    assert any("couldn't apply" in str(a).lower() for a, _k in query.answers)

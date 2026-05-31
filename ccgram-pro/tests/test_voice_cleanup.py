from __future__ import annotations

import asyncio
from typing import Any

import ccgram.llm as llm
from ccgram_pro.input_pipeline import voice_cleanup


class _Completer:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    async def complete(self, system_prompt: str, user_message: str) -> str:
        self.calls += 1
        return self.reply


def _use_completer(monkeypatch, completer: Any) -> None:
    monkeypatch.setattr(llm, "get_text_completer", lambda: completer)


async def test_clean_returns_raw_when_no_completer(monkeypatch) -> None:
    _use_completer(monkeypatch, None)
    assert await voice_cleanup.clean_transcript("use the modal") == "use the modal"


async def test_clean_fixes_homophone(monkeypatch) -> None:
    _use_completer(monkeypatch, _Completer("Use the Model class."))
    out = await voice_cleanup.clean_transcript("use the modal class")
    assert out == "Use the Model class."


async def test_clean_empty_short_circuits(monkeypatch) -> None:
    completer = _Completer("x")
    _use_completer(monkeypatch, completer)
    assert await voice_cleanup.clean_transcript("   ") == "   "
    assert completer.calls == 0


async def test_clean_falls_back_on_timeout(monkeypatch) -> None:
    monkeypatch.setattr(voice_cleanup, "_CLEANUP_TIMEOUT_S", 0.01)

    class _Slow:
        async def complete(self, system_prompt: str, user_message: str) -> str:
            await asyncio.sleep(1)
            return "never"

    _use_completer(monkeypatch, _Slow())
    assert await voice_cleanup.clean_transcript("hello world") == "hello world"


async def test_clean_falls_back_on_runtime_error(monkeypatch) -> None:
    class _Boom:
        async def complete(self, system_prompt: str, user_message: str) -> str:
            raise RuntimeError("api down")

    _use_completer(monkeypatch, _Boom())
    assert await voice_cleanup.clean_transcript("hello") == "hello"


async def test_clean_rejects_expansion(monkeypatch) -> None:
    _use_completer(monkeypatch, _Completer("an essay " * 100))
    raw = "fix the bug"
    assert await voice_cleanup.clean_transcript(raw) == raw


async def test_clean_strips_wrapping_quotes(monkeypatch) -> None:
    _use_completer(monkeypatch, _Completer('"Fix the model."'))
    assert await voice_cleanup.clean_transcript("fix the modal") == "Fix the model."


async def test_clean_strips_code_fence(monkeypatch) -> None:
    _use_completer(monkeypatch, _Completer("```\nFix the model.\n```"))
    assert await voice_cleanup.clean_transcript("fix the modal") == "Fix the model."


async def test_wrapped_transcribe_uses_cleaned_text(monkeypatch) -> None:
    from ccgram.whisper.base import TranscriptionResult
    from ccgram_pro.input_pipeline import intercept

    async def fake_original(message, transcriber, audio_bytes):  # noqa: ARG001
        return TranscriptionResult(text="use the modal", language="en")

    monkeypatch.setattr(intercept, "_ORIGINAL_TRANSCRIBE_AUDIO", fake_original)

    async def fake_clean(raw: str) -> str:
        return "use the model"

    monkeypatch.setattr(voice_cleanup, "clean_transcript", fake_clean)
    result = await intercept._wrapped_transcribe_audio(None, None, b"")
    assert result.text == "use the model"
    assert result.language == "en"

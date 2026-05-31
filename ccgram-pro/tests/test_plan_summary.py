from __future__ import annotations

import ccgram.llm as llm
from ccgram_pro.output_pipeline import plan_summary


class _Completer:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def complete(self, system_prompt: str, user_message: str) -> str:
        return self.reply


def test_first_idea_uses_heading_and_paragraph() -> None:
    md = "# Build the thing\n\nWe will add a module and wire it in.\n\n## Details\n- a\n- b"
    idea = plan_summary._first_idea(md)
    assert "Build the thing" in idea
    assert "add a module" in idea


def test_first_idea_strips_markdown() -> None:
    md = "## **Bold** plan\n\nUse `code` here."
    idea = plan_summary._first_idea(md)
    assert "*" not in idea and "`" not in idea and "#" not in idea


async def test_condense_uses_llm(monkeypatch) -> None:
    monkeypatch.setattr(llm, "get_text_completer", lambda: _Completer("Short idea."))
    out = await plan_summary.condense_plan("# Plan\n\nlong detail")
    assert out == "Short idea."


async def test_condense_falls_back_without_llm(monkeypatch) -> None:
    monkeypatch.setattr(llm, "get_text_completer", lambda: None)
    out = await plan_summary.condense_plan("# Heading\n\nBody paragraph here.")
    assert "Heading" in out


async def test_condense_falls_back_on_error(monkeypatch) -> None:
    class _Boom:
        async def complete(self, system_prompt: str, user_message: str) -> str:
            raise RuntimeError("down")

    monkeypatch.setattr(llm, "get_text_completer", lambda: _Boom())
    out = await plan_summary.condense_plan("# Heading\n\nBody.")
    assert "Heading" in out


async def test_condense_empty_plan() -> None:
    out = await plan_summary.condense_plan("   ")
    assert "review" in out.lower()

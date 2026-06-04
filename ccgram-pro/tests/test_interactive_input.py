from __future__ import annotations

import json
from pathlib import Path

from ccgram_pro.output_pipeline import interactive_input


def _line(**obj) -> str:
    return json.dumps(obj)


def _ask(tool_id: str, options: list[str], multi: bool = False) -> str:
    return _line(
        type="assistant",
        message={
            "content": [
                {
                    "type": "tool_use",
                    "name": "AskUserQuestion",
                    "id": tool_id,
                    "input": {
                        "questions": [
                            {
                                "question": "Pick one",
                                "options": [{"label": o} for o in options],
                                "multiSelect": multi,
                            }
                        ]
                    },
                }
            ]
        },
    )


def _result(tool_id: str) -> str:
    return _line(
        type="user",
        message={
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}
            ]
        },
    )


def _plan(tool_id: str, plan: str) -> str:
    return _line(
        type="assistant",
        message={
            "content": [
                {
                    "type": "tool_use",
                    "name": "ExitPlanMode",
                    "id": tool_id,
                    "input": {"plan": plan},
                }
            ]
        },
    )


def _write(tmp_path: Path, *lines: str) -> str:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def test_read_single_question(tmp_path: Path) -> None:
    path = _write(tmp_path, _ask("t1", ["Yes", "No"]))
    active = interactive_input.read_active_prompt(path)
    assert active is not None and active[0] == "ask"
    q = active[1]
    assert q.options == ["Yes", "No"]
    assert q.multi_select is False
    assert q.tool_use_id == "t1"


def test_read_multiselect(tmp_path: Path) -> None:
    path = _write(tmp_path, _ask("t1", ["A", "B", "C"], multi=True))
    active = interactive_input.read_active_prompt(path)
    assert active is not None and active[1].multi_select is True


def _tool(tool_id: str, name: str) -> str:
    return _line(
        type="assistant",
        message={
            "content": [
                {"type": "tool_use", "name": name, "id": tool_id, "input": {"x": 1}}
            ]
        },
    )


def test_active_prompt_ask_when_last_tool_use(tmp_path: Path) -> None:
    path = _write(tmp_path, _ask("t1", ["A", "B"]))
    active = interactive_input.read_active_prompt(path)
    assert active is not None and active[0] == "ask"
    assert active[1].options == ["A", "B"]


def test_active_prompt_plan_when_last_tool_use(tmp_path: Path) -> None:
    path = _write(tmp_path, _plan("p1", "# Plan\n\nbody"))
    active = interactive_input.read_active_prompt(path)
    assert active is not None and active[0] == "plan"
    assert active[1] == "# Plan\n\nbody"


def test_active_prompt_none_for_permission_tool(tmp_path: Path) -> None:
    # An unanswered AskUserQuestion followed by a later Bash (permission-gated)
    # → the live prompt is the Bash permission, NOT the old question.
    path = _write(tmp_path, _ask("t1", ["A", "B"]), _tool("b1", "Bash"))
    assert interactive_input.read_active_prompt(path) is None


def test_active_prompt_none_when_answered(tmp_path: Path) -> None:
    path = _write(tmp_path, _ask("t1", ["A", "B"]), _result("t1"))
    assert interactive_input.read_active_prompt(path) is None


_PANE_ASK = (
    "  The reset is the start of the ISO week.\n"
    " ☐ Reset wording\n"
    "\n"
    "The reset is actually the start of Monday (UTC), not Sunday midnight. How should\n"
    " I word the tooltip?\n"
    "\n"
    "❯ 1. Resets every Monday\n"
    "     Accurate to the ISO-week start.\n"
    "  2. Resets weekly\n"
    "     Vaguer but safe.\n"
    "  3. Drop the reset sentence\n"
    "  4. Keep Sunday midnight\n"
    "  5. Type something.\n"
    "  6. Chat about this\n"
    "\n"
    "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
)

_PANE_PERMISSION = (
    "Do you want to make this edit to reward.ts?\n"
    "❯ 1. Yes\n"
    "  2. Yes, and don't ask again\n"
    "  3. No\n"
    "\n"
    "Esc to cancel\n"
)


def test_parse_pane_prompt_extracts_question_and_options() -> None:
    active = interactive_input.parse_pane_prompt(_PANE_ASK)
    assert active is not None and active[0] == "ask"
    q = active[1]
    assert q.options == [
        "Resets every Monday",
        "Resets weekly",
        "Drop the reset sentence",
        "Keep Sunday midnight",
        "Type something.",
        "Chat about this",
    ]
    assert q.multi_select is False
    assert q.tool_use_id == ""


def test_parse_pane_prompt_joins_wrapped_question_body() -> None:
    q = interactive_input.parse_pane_prompt(_PANE_ASK)[1]
    assert "Reset wording" in q.question
    assert "How should I word the tooltip?" in q.question
    assert "How should\n I word" not in q.question


def test_parse_pane_prompt_ignores_permission_prompt() -> None:
    assert interactive_input.parse_pane_prompt(_PANE_PERMISSION) is None


def test_parse_pane_prompt_empty_is_none() -> None:
    assert interactive_input.parse_pane_prompt("") is None
    assert interactive_input.parse_pane_prompt("just some output\n") is None

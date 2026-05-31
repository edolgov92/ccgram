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

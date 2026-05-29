"""Tests for the structured transcript extractor + HTML renderer."""

from __future__ import annotations

import json
from pathlib import Path

from ccgram_pro.output_pipeline.transcript_events import (
    TurnEvent,
    events_from_dicts,
    events_to_dicts,
    extract_events,
)
from ccgram_pro.web.transcript_render import render_events_html, transcript_css


def _write_transcript(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")


def test_extract_events_user_and_assistant_text(tmp_path: Path) -> None:
    f = tmp_path / "t.jsonl"
    _write_transcript(
        f,
        [
            {"role": "user", "message": {"content": "hi"}},
            {
                "role": "assistant",
                "message": {"content": [{"type": "text", "text": "Hello!"}]},
            },
        ],
    )
    events = extract_events(str(f))
    assert [e.kind for e in events] == ["user", "assistant"]
    assert events[0].text == "hi"
    assert events[1].text == "Hello!"


def test_extract_events_tool_use_and_result(tmp_path: Path) -> None:
    f = tmp_path / "t.jsonl"
    _write_transcript(
        f,
        [
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "id": "tu1",
                            "input": {"command": "ls -la", "description": "list"},
                        }
                    ]
                },
            },
            {
                "role": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu1",
                            "content": "file1\nfile2",
                        }
                    ]
                },
            },
        ],
    )
    events = extract_events(str(f))
    assert events[0].kind == "tool_use"
    assert events[0].tool_name == "Bash"
    assert events[0].tool_input == {"command": "ls -la", "description": "list"}
    assert events[1].kind == "tool_result"
    assert events[1].tool_use_id == "tu1"
    assert "file1" in events[1].text


def test_extract_events_thinking(tmp_path: Path) -> None:
    f = tmp_path / "t.jsonl"
    _write_transcript(
        f,
        [
            {
                "role": "assistant",
                "message": {
                    "content": [{"type": "thinking", "thinking": "let me think"}]
                },
            }
        ],
    )
    events = extract_events(str(f))
    assert events[0].kind == "thinking"
    assert "let me think" in events[0].text


def test_extract_events_tool_result_list_content(tmp_path: Path) -> None:
    f = tmp_path / "t.jsonl"
    _write_transcript(
        f,
        [
            {
                "role": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": [{"type": "text", "text": "out"}],
                            "is_error": True,
                        }
                    ]
                },
            }
        ],
    )
    events = extract_events(str(f))
    assert events[0].kind == "tool_result"
    assert events[0].text == "out"
    assert events[0].is_error is True


def test_extract_events_missing_file_returns_empty() -> None:
    assert extract_events("/nonexistent/path.jsonl") == []


def test_extract_events_skips_malformed_lines(tmp_path: Path) -> None:
    f = tmp_path / "t.jsonl"
    f.write_text('not json\n{"role":"user","message":{"content":"ok"}}\n')
    events = extract_events(str(f))
    assert len(events) == 1
    assert events[0].text == "ok"


def test_events_dict_round_trip() -> None:
    events = [
        TurnEvent(kind="user", text="hi"),
        TurnEvent(
            kind="tool_use",
            tool_name="Bash",
            tool_input={"command": "ls"},
            tool_use_id="x",
        ),
        TurnEvent(kind="tool_result", text="out", tool_use_id="x", is_error=True),
    ]
    dicts = events_to_dicts(events)
    restored = events_from_dicts(dicts)
    assert [e.kind for e in restored] == ["user", "tool_use", "tool_result"]
    assert restored[1].tool_input == {"command": "ls"}
    assert restored[2].is_error is True


# ── HTML rendering ──────────────────────────────────────────────────────


def test_render_distinguishes_roles() -> None:
    events = [
        TurnEvent(kind="user", text="do a thing"),
        TurnEvent(kind="assistant", text="done"),
    ]
    html = render_events_html(events)
    assert 'class="row user"' in html
    assert 'class="row assistant"' in html
    assert ">You<" in html
    assert ">Claude<" in html


def test_render_tool_use_shows_name_and_command() -> None:
    events = [
        TurnEvent(
            kind="tool_use",
            tool_name="Bash",
            tool_input={"command": "git status", "description": "check"},
        )
    ]
    html = render_events_html(events)
    assert "Bash" in html
    assert "git status" in html  # headline command surfaced
    assert "tool-bubble" in html


def test_render_tool_result_collapses_long_output() -> None:
    long_output = "x" * 5000
    events = [TurnEvent(kind="tool_result", text=long_output)]
    html = render_events_html(events)
    assert "show" in html and "more chars" in html
    assert "<details" in html


def test_render_error_result_gets_error_class() -> None:
    events = [TurnEvent(kind="tool_result", text="boom", is_error=True)]
    html = render_events_html(events)
    assert "error" in html


def test_render_escapes_html_in_text() -> None:
    events = [TurnEvent(kind="user", text="<script>alert(1)</script>")]
    html = render_events_html(events)
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;" in html


def test_render_assistant_fenced_code() -> None:
    events = [TurnEvent(kind="assistant", text="here:\n```\nprint(1)\n```")]
    html = render_events_html(events)
    assert "<pre><code>" in html
    assert "print(1)" in html


def test_render_empty_events() -> None:
    assert "No transcript content" in render_events_html([])


def test_transcript_css_is_nonempty_and_brace_balanced() -> None:
    css = transcript_css()
    assert ".transcript" in css
    assert ".tool-bubble" in css
    # The CSS is interpolated as a format() VALUE, not template — but make
    # sure braces are balanced so it's valid CSS.
    assert css.count("{") == css.count("}")

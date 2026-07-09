from __future__ import annotations

import json

import pytest
from ccgram_pro.share.tokens import (
    InvalidShareToken,
    sign_live_token,
    verify_live_token,
    verify_share_token,
)
from ccgram_pro.web.routes_live import _read_new_events


def _line(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    )


def test_read_new_events_incremental(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_line("first") + "\n", encoding="utf-8")
    events, offset = _read_new_events(str(p), 0)
    assert [e.text for e in events] == ["first"]
    assert offset == p.stat().st_size

    with open(p, "a", encoding="utf-8") as fh:
        fh.write(_line("second") + "\n")
    events2, offset2 = _read_new_events(str(p), offset)
    assert [e.text for e in events2] == ["second"]
    assert offset2 == p.stat().st_size


def test_read_new_events_holds_partial_line(tmp_path):
    p = tmp_path / "t.jsonl"
    complete = _line("done") + "\n"
    p.write_text(complete + '{"partial', encoding="utf-8")
    events, offset = _read_new_events(str(p), 0)
    assert [e.text for e in events] == ["done"]
    assert offset == len(complete.encode("utf-8"))  # partial re-read next poll


def test_read_new_events_resets_on_truncation(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_line("a") + "\n", encoding="utf-8")
    events, _off = _read_new_events(str(p), 999_999)  # offset past EOF
    assert [e.text for e in events] == ["a"]


def test_read_new_events_missing_path():
    assert _read_new_events(None, 0) == ([], 0)
    assert _read_new_events("/no/such/file.jsonl", 5) == ([], 5)


def test_live_token_roundtrip():
    tok = sign_live_token(bot_token="bt", window_id="@7")
    assert verify_live_token(tok, bot_token="bt").share_id == "@7"


def test_live_token_rejected_as_share():
    tok = sign_live_token(bot_token="bt", window_id="@7")
    with pytest.raises(InvalidShareToken):
        verify_share_token(tok, bot_token="bt")


def test_live_token_wrong_bot_token_rejected():
    tok = sign_live_token(bot_token="bt", window_id="@7")
    with pytest.raises(InvalidShareToken):
        verify_live_token(tok, bot_token="other")

from __future__ import annotations

from ccgram_pro.web.live_status import derive_status, terminal_tail


def test_working_from_esc_to_interrupt():
    pane = (
        "● reading files\n"
        "✻ Crafting… (2m 43s · ↓ 21.9k tokens · thinking with xhigh effort)\n"
        "⏵⏵ bypass permissions on · esc to interrupt · ← for age"
    )
    s = derive_status(pane)
    assert s.state == "working"
    assert "2m 43s" in s.detail
    assert s.overlay is False


def test_idle_from_ready_prompt():
    pane = "● Done.\n❯ \n⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
    s = derive_status(pane)
    assert s.state == "idle"
    assert s.overlay is False
    assert s.label == "Idle"


def test_overlay_usage_screen_flags_stuck():
    pane = (
        "Settings  Status   Config   Usage   Stats\n"
        "Current session\n5% used\n❯ \n"
        "⏵⏵ bypass permissions · ← for agents"
    )
    s = derive_status(pane)
    assert s.state == "idle"
    assert s.overlay is True
    assert "screen is open" in s.label.lower()


def test_awaiting_from_numbered_options():
    pane = "Switch model?\n❯ 1. Yes, switch to Opus\n  2. No, go back"
    s = derive_status(pane)
    assert s.state == "awaiting"
    assert "1." in s.detail


def test_awaiting_from_permission_phrase():
    pane = "Edit file.py?\nDo you want to make this edit?\n"
    s = derive_status(pane)
    assert s.state == "awaiting"


def test_working_beats_overlay_signature():
    pane = "Current session 5% used\n✻ Churning (9s · thinking with high effort)\n· esc to interrupt"
    s = derive_status(pane)
    assert s.state == "working"
    assert s.overlay is False


def test_empty_pane_is_idle():
    assert derive_status("").state == "idle"
    assert derive_status(None).state == "idle"  # type: ignore[arg-type]


def test_terminal_tail_strips_blanks_and_trailing_space():
    assert terminal_tail("a\n\nb\n c \nd", 2) == [" c", "d"]
    assert terminal_tail("", 5) == []

from __future__ import annotations

import re

import pytest
from ccgram_pro import usage


@pytest.fixture(autouse=True)
def _reset():
    usage._reset_cache_for_testing()
    yield
    usage._reset_cache_for_testing()


_SAMPLE = {
    "five_hour": {"utilization": 6.0, "resets_at": "2026-07-09T13:19:59+00:00"},
    "seven_day": {"utilization": 63.0, "resets_at": "2026-07-13T11:59:59+00:00"},
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 6,
            "resets_at": "x",
            "scope": None,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 63,
            "resets_at": "x",
            "scope": None,
        },
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 100,
            "resets_at": "2026-07-13T11:59:59+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}},
        },
    ],
}


def test_format_account_limits_full():
    line = usage.format_account_limits(_SAMPLE)
    assert "5h: 6%" in line
    assert "Weekly: 63%" in line
    assert "Fable: 100%" in line
    assert line.count("Fable") == 1  # session + weekly_all don't add model lines
    assert "res " in line


def test_format_account_limits_handles_missing_fields():
    assert usage.format_account_limits({}) == ""
    assert usage.format_account_limits({"five_hour": {"utilization": None}}) == ""


def test_fmt_reset_time_only():
    assert re.fullmatch(
        r"\d{1,2}:\d{2}(am|pm)",
        usage._fmt_reset("2026-07-09T13:19:59+00:00", with_date=False),
    )


def test_fmt_reset_with_date():
    assert re.fullmatch(
        r"\d{1,2} [A-Z][a-z]{2} \d{1,2}:\d{2}(am|pm)",
        usage._fmt_reset("2026-07-13T11:59:59+00:00", with_date=True),
    )


def test_fmt_reset_bad_input():
    assert usage._fmt_reset(None, with_date=False) == ""
    assert usage._fmt_reset("not-a-date", with_date=True) == ""


def test_context_window_mapping():
    assert usage._context_window("opus48") == 200_000  # legacy Opus 4.8
    assert usage._context_window("opus48-1m") == 1_000_000
    assert usage._context_window("opus5") == 1_000_000  # Opus 5 native 1M
    assert usage._context_window("claude-opus-5") == 1_000_000
    assert usage._context_window("opus5-1m") == 1_000_000
    assert usage._context_window("fable5") == 1_000_000
    assert usage._context_window("claude-fable-5") == 1_000_000
    assert usage._context_window("") == 200_000


async def test_build_footer_combines(monkeypatch):
    monkeypatch.setattr(usage, "_context_percent", lambda _wid: 43)

    async def fake_fetch():
        return {
            "five_hour": {"utilization": 6.0, "resets_at": None},
            "seven_day": {"utilization": 63.0, "resets_at": None},
            "limits": [],
        }

    monkeypatch.setattr(usage, "_fetch_usage", fake_fetch)
    footer = await usage.build_footer("@1")
    assert footer.startswith("\n\n")
    assert "Context: 43%" in footer
    assert "5h: 6%" in footer
    assert "Weekly: 63%" in footer


async def test_build_footer_context_only_when_usage_none(monkeypatch):
    monkeypatch.setattr(usage, "_context_percent", lambda _wid: 12)

    async def none_fetch():
        return None

    monkeypatch.setattr(usage, "_fetch_usage", none_fetch)
    footer = await usage.build_footer("@1")
    assert footer == "\n\nContext: 12%"


async def test_build_footer_empty_when_nothing(monkeypatch):
    monkeypatch.setattr(usage, "_context_percent", lambda _wid: None)

    async def none_fetch():
        return None

    monkeypatch.setattr(usage, "_fetch_usage", none_fetch)
    assert await usage.build_footer("@1") == ""


async def test_build_footer_never_raises(monkeypatch):
    def boom(_wid):
        raise RuntimeError("boom")

    monkeypatch.setattr(usage, "_context_percent", boom)
    assert await usage.build_footer("@1") == ""

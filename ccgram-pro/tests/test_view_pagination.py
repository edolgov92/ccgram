"""Tests for the transcript view's pagination + infinite-scroll endpoint."""

from __future__ import annotations

import json

from aiohttp.test_utils import TestClient, TestServer
from ccgram.miniapp.server import build_app
from ccgram_pro.share.store import save_share
from ccgram_pro.share.tokens import sign_share_token
from ccgram_pro.web import register_diff_routes, register_view_routes
from ccgram_pro.web.routes_view import _clamp_limit, _window

_BOT = "12345:test-bot-token-aaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _make_transcript_share(n_events: int) -> str:
    events = [
        {"kind": "user" if i % 2 == 0 else "assistant", "text": f"msg {i}"}
        for i in range(n_events)
    ]
    body = json.dumps({"v": 2, "num_turns": n_events, "events": events})
    return save_share(kind="claude-turn", title="t", body_markdown=body)


# ── pure helpers ─────────────────────────────────────────────────────────


def test_clamp_limit_default_and_bounds() -> None:
    assert _clamp_limit(None) == 20
    assert _clamp_limit("5") == 5
    assert _clamp_limit("0") == 1  # floor
    assert _clamp_limit("9999") == 100  # ceiling
    assert _clamp_limit("garbage") == 20


def test_window_newest_page() -> None:
    events = [{"i": i} for i in range(50)]
    window, start = _window(events, before=None, limit=20)
    assert start == 30
    assert window == [{"i": i} for i in range(30, 50)]


def test_window_older_page() -> None:
    events = [{"i": i} for i in range(50)]
    window, start = _window(events, before=30, limit=20)
    assert start == 10
    assert window == [{"i": i} for i in range(10, 30)]


def test_window_reaches_start() -> None:
    events = [{"i": i} for i in range(15)]
    window, start = _window(events, before=10, limit=20)
    assert start == 0
    assert window == [{"i": i} for i in range(0, 10)]


def test_window_clamps_out_of_range_before() -> None:
    events = [{"i": i} for i in range(10)]
    window, start = _window(events, before=999, limit=5)
    assert start == 5
    assert len(window) == 5


# ── HTTP flow ──────────────────────────────────────────────────────────────


class _ViewServer:
    """aiohttp test server with the view + diff routes registered."""

    async def __aenter__(self):
        app = build_app(bot_token=_BOT)
        register_view_routes(app)
        register_diff_routes(app)
        self.server = TestServer(app)
        self.client = TestClient(self.server)
        await self.client.start_server()
        return self.client

    async def __aexit__(self, *exc):
        await self.client.close()


async def test_view_shows_only_newest_page() -> None:
    share_id = _make_transcript_share(50)
    token = sign_share_token(bot_token=_BOT, share_id=share_id)
    async with _ViewServer() as client, client.get(f"/view/{token}") as resp:
        assert resp.status == 200
        body = await resp.text()
    # Newest page = msgs 30..49 present, msg 0..29 absent.
    assert "msg 49" in body
    assert "msg 30" in body
    assert "msg 29" not in body
    assert "msg 0<" not in body
    # The "Load older" sentinel + infinite-scroll JS are present.
    assert 'id="load-older"' in body
    assert "IntersectionObserver" in body
    assert "50 message(s)" in body


async def test_view_no_older_link_for_short_chat() -> None:
    share_id = _make_transcript_share(5)
    token = sign_share_token(bot_token=_BOT, share_id=share_id)
    async with _ViewServer() as client, client.get(f"/view/{token}") as resp:
        body = await resp.text()
    assert "msg 0" in body and "msg 4" in body
    assert 'id="load-older"' not in body  # nothing older to load


async def test_view_before_param_renders_older_window() -> None:
    share_id = _make_transcript_share(50)
    token = sign_share_token(bot_token=_BOT, share_id=share_id)
    async with _ViewServer() as client:
        async with client.get(f"/view/{token}?before=30&limit=20") as resp:
            body = await resp.text()
    assert "msg 10" in body and "msg 29" in body
    assert "msg 49" not in body


async def test_older_fragment_returns_json_rows() -> None:
    share_id = _make_transcript_share(50)
    token = sign_share_token(bot_token=_BOT, share_id=share_id)
    async with _ViewServer() as client:
        async with client.get(f"/view/{token}/older?before=30&limit=20") as resp:
            assert resp.status == 200
            data = await resp.json()
    assert data["oldest"] == 10
    assert "msg 10" in data["rows"]
    assert "msg 29" in data["rows"]
    assert "msg 30" not in data["rows"]


async def test_older_fragment_signals_start() -> None:
    share_id = _make_transcript_share(15)
    token = sign_share_token(bot_token=_BOT, share_id=share_id)
    async with _ViewServer() as client:
        async with client.get(f"/view/{token}/older?before=10&limit=20") as resp:
            data = await resp.json()
    assert data["oldest"] == 0  # reached the start


async def test_older_fragment_rejects_bad_token() -> None:
    async with _ViewServer() as client:
        async with client.get("/view/not-a-token/older?before=5") as resp:
            assert resp.status == 403


async def test_view_rejects_expired_or_bad_token() -> None:
    async with _ViewServer() as client, client.get("/view/garbage") as resp:
        assert resp.status == 403


async def test_view_includes_copy_buttons_js_and_css() -> None:
    share_id = _make_transcript_share(5)
    token = sign_share_token(bot_token=_BOT, share_id=share_id)
    async with _ViewServer() as client, client.get(f"/view/{token}") as resp:
        body = await resp.text()
    assert "copy-btn" in body
    assert "copyText" in body
    assert "MutationObserver" in body
    assert "navigator.clipboard" in body
    assert ".prewrap" in body


async def test_legacy_markdown_share_also_gets_copy_js() -> None:
    share_id = save_share(kind="claude-turn", title="t", body_markdown="# hi\n`x`")
    token = sign_share_token(bot_token=_BOT, share_id=share_id)
    async with _ViewServer() as client, client.get(f"/view/{token}") as resp:
        body = await resp.text()
    assert "<article>" in body
    assert "copy-btn" in body
    assert "copyText" in body

from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer
from ccgram.miniapp.server import build_app
from ccgram_pro.share.store import save_share
from ccgram_pro.share.tokens import sign_share_token
from ccgram_pro.web import register_plan_routes
from ccgram_pro.web.transcript_render import render_plan_markdown

_BOT = "12345:test-bot-token-aaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_render_plan_markdown_headings_and_lists() -> None:
    html = render_plan_markdown("# Title\n\nIntro para.\n\n- one\n- two\n\n1. first")
    assert "<h2>Title</h2>" in html
    assert "<p>Intro para.</p>" in html
    assert "<ul>" in html and "<li>one</li>" in html
    assert "<ol>" in html and "<li>first</li>" in html


def test_render_plan_escapes_html() -> None:
    html = render_plan_markdown("Do <script>alert(1)</script> now")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_plan_fenced_code() -> None:
    html = render_plan_markdown("Run:\n\n```\nmake check\n```")
    assert "<pre><code>make check</code></pre>" in html


class _PlanServer:
    async def __aenter__(self):
        app = build_app(bot_token=_BOT)
        register_plan_routes(app)
        self.server = TestServer(app)
        self.client = TestClient(self.server)
        await self.client.start_server()
        return self.client

    async def __aexit__(self, *exc):
        await self.client.close()


async def test_plan_page_renders() -> None:
    share_id = save_share(
        kind="plan", title="Plan", body_markdown="# Build it\n\nStep one."
    )
    token = sign_share_token(bot_token=_BOT, share_id=share_id)
    async with _PlanServer() as client, client.get(f"/plan/{token}") as resp:
        assert resp.status == 200
        body = await resp.text()
    assert "Build it" in body
    assert "Step one." in body


async def test_plan_page_bad_token_403() -> None:
    async with _PlanServer() as client, client.get("/plan/garbage") as resp:
        assert resp.status == 403


async def test_plan_page_rejects_non_plan_kind() -> None:
    share_id = save_share(kind="summary", title="x", body_markdown="not a plan")
    token = sign_share_token(bot_token=_BOT, share_id=share_id)
    async with _PlanServer() as client, client.get(f"/plan/{token}") as resp:
        assert resp.status == 404

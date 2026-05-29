"""Tests for the share-link subsystem — tokens, store, view route."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer
from ccgram.miniapp.server import _BOT_TOKEN_KEY  # type: ignore[attr-defined]
from ccgram_pro.share import (
    InvalidShareToken,
    ShareNotFound,
    load_share,
    prune_expired,
    save_share,
    sign_share_token,
    verify_share_token,
)
from ccgram_pro.share.links import make_share_url, resolve_token
from ccgram_pro.share.store import shares_dir
from ccgram_pro.web import register_view_routes


_BOT = "12345:fake-bot-token"


# ── tokens ─────────────────────────────────────────────────────────────


def test_sign_and_verify_round_trip() -> None:
    token = sign_share_token(bot_token=_BOT, share_id="abc123")
    payload = verify_share_token(token, bot_token=_BOT)
    assert payload.share_id == "abc123"
    assert payload.purpose == "share"
    assert payload.exp > time.time()


def test_verify_rejects_wrong_bot_token() -> None:
    token = sign_share_token(bot_token=_BOT, share_id="abc")
    with pytest.raises(InvalidShareToken, match="signature"):
        verify_share_token(token, bot_token="different")


def test_verify_rejects_malformed_token() -> None:
    with pytest.raises(InvalidShareToken):
        verify_share_token("not.a.real.token", bot_token=_BOT)
    with pytest.raises(InvalidShareToken):
        verify_share_token("", bot_token=_BOT)


def test_verify_rejects_expired() -> None:
    token = sign_share_token(bot_token=_BOT, share_id="abc", ttl=10)
    with pytest.raises(InvalidShareToken, match="expired"):
        verify_share_token(token, bot_token=_BOT, now=time.time() + 1000)


def test_verify_rejects_wrong_purpose_via_replay() -> None:
    """Tampering the purpose claim must produce a signature mismatch."""
    import base64

    token = sign_share_token(bot_token=_BOT, share_id="abc")
    body_b64, sig_b64 = token.split(".")
    body = json.loads(base64.urlsafe_b64decode(body_b64 + "==").decode())
    body["p"] = "window"  # try to replay as a window token
    tampered_body = (
        base64.urlsafe_b64encode(
            json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    with pytest.raises(InvalidShareToken):
        verify_share_token(f"{tampered_body}.{sig_b64}", bot_token=_BOT)


def test_sign_rejects_empty_share_id() -> None:
    with pytest.raises(InvalidShareToken):
        sign_share_token(bot_token=_BOT, share_id="")


# ── store ──────────────────────────────────────────────────────────────


def test_save_and_load_share_round_trip(tmp_path: Path) -> None:
    share_id = save_share(
        kind="claude-turn",
        title="What is in the README?",
        body_markdown="# ccgram\n\nThe README is here.",
        window_id="@5",
    )
    assert share_id and len(share_id) > 8
    record = load_share(share_id)
    assert record.kind == "claude-turn"
    assert record.title == "What is in the README?"
    assert "The README is here." in record.body_markdown
    assert record.window_id == "@5"
    assert record.created_at > 0


def test_save_share_stores_under_layer_dir(tmp_path: Path) -> None:
    share_id = save_share(kind="raw", title="t", body_markdown="b")
    assert (shares_dir() / share_id / "meta.json").is_file()
    assert (shares_dir() / share_id / "content.md").is_file()


def test_load_share_raises_on_missing() -> None:
    with pytest.raises(ShareNotFound):
        load_share("definitely-not-real-id")


def test_load_share_rejects_path_traversal() -> None:
    with pytest.raises(ShareNotFound):
        load_share("../etc/passwd")


def test_prune_removes_old_shares(tmp_path: Path) -> None:
    share_id = save_share(kind="raw", title="t", body_markdown="b")
    meta_path = shares_dir() / share_id / "meta.json"
    # Backdate the share so it falls past the prune cutoff.
    meta = json.loads(meta_path.read_text())
    meta["created_at"] = time.time() - 30 * 86400
    meta_path.write_text(json.dumps(meta))
    removed = prune_expired(max_age_seconds=7 * 86400)
    assert removed == 1
    with pytest.raises(ShareNotFound):
        load_share(share_id)


def test_prune_skips_fresh_shares(tmp_path: Path) -> None:
    share_id = save_share(kind="raw", title="t", body_markdown="b")
    removed = prune_expired(max_age_seconds=7 * 86400)
    assert removed == 0
    assert load_share(share_id).share_id == share_id


# ── links ──────────────────────────────────────────────────────────────


def test_make_share_url_returns_none_without_base(monkeypatch) -> None:
    monkeypatch.delenv("CCGRAM_MINIAPP_BASE_URL", raising=False)
    monkeypatch.delenv("CCGRAM_MINIAPP_BASE_URL_PENDING", raising=False)
    assert make_share_url(bot_token=_BOT, share_id="abc") is None


def test_make_share_url_uses_pending_fallback(monkeypatch) -> None:
    monkeypatch.setenv("CCGRAM_MINIAPP_BASE_URL", "")
    monkeypatch.setenv("CCGRAM_MINIAPP_BASE_URL_PENDING", "https://t.example.com")
    url = make_share_url(bot_token=_BOT, share_id="abc")
    assert url is not None
    assert url.startswith("https://t.example.com/view/")


def test_make_share_url_strips_trailing_slash(monkeypatch) -> None:
    monkeypatch.setenv("CCGRAM_MINIAPP_BASE_URL", "https://x.example.com/")
    url = make_share_url(bot_token=_BOT, share_id="abc")
    assert url is not None
    assert url.startswith("https://x.example.com/view/")
    assert "//view" not in url


def test_resolve_token_round_trip(tmp_path: Path) -> None:
    share_id = save_share(kind="raw", title="t", body_markdown="hello world")
    token = sign_share_token(bot_token=_BOT, share_id=share_id)
    record = resolve_token(token, bot_token=_BOT)
    assert record.share_id == share_id
    assert "hello world" in record.body_markdown


# ── view route ─────────────────────────────────────────────────────────


class TestViewRoute(AioHTTPTestCase):
    """End-to-end aiohttp test of the /view/{token} handler."""

    async def get_application(self) -> web.Application:
        app = web.Application()
        app[_BOT_TOKEN_KEY] = _BOT
        register_view_routes(app)
        return app

    async def test_get_view_renders_share(self) -> None:
        share_id = save_share(
            kind="claude-turn",
            title="My turn",
            body_markdown="Hello **bold** `code`",
            window_id="@7",
        )
        token = sign_share_token(bot_token=_BOT, share_id=share_id)
        async with self.client.get(f"/view/{token}") as resp:
            assert resp.status == 200
            body = await resp.text()
            assert "My turn" in body
            assert "code" in body
            assert "@7" in body

    async def test_get_view_rejects_bad_token(self) -> None:
        async with self.client.get("/view/not.a.token") as resp:
            assert resp.status == 403

    async def test_get_view_404_on_missing_share(self) -> None:
        token = sign_share_token(bot_token=_BOT, share_id="this-id-not-saved")
        async with self.client.get(f"/view/{token}") as resp:
            assert resp.status == 404

    async def test_get_view_escapes_xss(self) -> None:
        """All untrusted HTML must be ``html.escape``-d, never rendered live."""
        share_id = save_share(
            kind="raw",
            title="<script>alert(1)</script>",
            body_markdown="<img src=x onerror=alert(2)>",
        )
        token = sign_share_token(bot_token=_BOT, share_id=share_id)
        async with self.client.get(f"/view/{token}") as resp:
            body = (
                await resp.text().__aiter__().__anext__()
                if False
                else await resp.text()
            )
            # No live tags from user content — both title and body content
            # are rendered as escaped text, not as HTML.
            assert "<script>alert(1)" not in body
            assert "<img src=x" not in body.lower()
            # The escaped form proves the input was passed through html.escape.
            assert "&lt;script&gt;alert(1)" in body
            assert "&lt;img src=x" in body.lower()


# Silence unused-import warning — TestClient/TestServer are imported so the
# AioHTTPTestCase base class can resolve them via aiohttp.test_utils.
_ = (TestClient, TestServer)

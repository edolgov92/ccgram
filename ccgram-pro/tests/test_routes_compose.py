from __future__ import annotations

import re

import pytest
from aiohttp.test_utils import TestClient, TestServer
from ccgram.miniapp.server import build_app
from ccgram_pro import state
from ccgram_pro.share import csrf
from ccgram_pro.share.tokens import sign_compose_token, sign_share_token
from ccgram_pro.web import register_compose_routes
from ccgram_pro.web import routes_compose as rc

_BOT = "12345:test-bot-token-aaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def _reset():
    csrf._reset_for_testing()
    yield
    csrf._reset_for_testing()


class _ComposeServer:
    async def __aenter__(self):
        app = build_app(bot_token=_BOT)
        register_compose_routes(app)
        self.server = TestServer(app)
        self.client = TestClient(self.server)
        await self.client.start_server()
        return self.client

    async def __aexit__(self, *exc):
        await self.client.close()


def _stub_repo(monkeypatch, *, ready: bool = True, head: str = "feat") -> None:
    monkeypatch.setattr(rc, "_resolve_repo", lambda wid: "/proj")
    monkeypatch.setattr(rc, "_default_base", lambda repo, branches: "main")

    import ccgram_pro.git_ops as ops

    monkeypatch.setattr(ops, "is_git_repo", lambda repo: True)
    monkeypatch.setattr(ops, "current_branch", lambda repo: head)

    class _B:
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(ops, "list_branches", lambda repo: [_B("main"), _B(head)])

    def _preflight(repo, *, base, head):  # noqa: ARG001
        if not ready:
            raise ops.PRValidationError("not pushed", hint="push first")

    monkeypatch.setattr(ops, "preflight_pull_request", _preflight)


async def test_share_token_rejected_by_compose() -> None:
    bad = sign_share_token(bot_token=_BOT, share_id="@5")
    async with _ComposeServer() as client, client.get(f"/compose/{bad}") as resp:
        assert resp.status == 403


async def test_get_renders_form_with_nonce(monkeypatch) -> None:
    _stub_repo(monkeypatch)
    token = sign_compose_token(bot_token=_BOT, window_id="@5")
    async with _ComposeServer() as client, client.get(f"/compose/{token}") as resp:
        assert resp.status == 200
        body = await resp.text()
    assert 'name="csrf"' in body
    assert "Open pull request" in body


async def test_get_not_ready_returns_409(monkeypatch) -> None:
    _stub_repo(monkeypatch, ready=False)
    token = sign_compose_token(bot_token=_BOT, window_id="@5")
    async with _ComposeServer() as client, client.get(f"/compose/{token}") as resp:
        assert resp.status == 409


async def test_post_without_nonce_rejected(monkeypatch) -> None:
    _stub_repo(monkeypatch)
    token = sign_compose_token(bot_token=_BOT, window_id="@5")
    async with _ComposeServer() as client:
        async with client.post(
            f"/compose/{token}/pr", data={"title": "x", "csrf": "bogus"}
        ) as resp:
            assert resp.status == 403


async def test_post_creates_pr_with_valid_nonce(monkeypatch) -> None:
    _stub_repo(monkeypatch)
    import ccgram_pro.git_ops as ops

    monkeypatch.setattr(
        ops, "create_pull_request", lambda *a, **k: "https://github.com/x/y/pull/1"
    )
    token = sign_compose_token(bot_token=_BOT, window_id="@5")
    async with _ComposeServer() as client:
        async with client.get(f"/compose/{token}") as resp:
            body = await resp.text()
        nonce = re.search(r'name="csrf" value="([^"]+)"', body).group(1)
        async with client.post(
            f"/compose/{token}/pr",
            data={"title": "My PR", "body": "b", "base": "main", "csrf": nonce},
        ) as resp:
            assert resp.status == 200
            out = await resp.text()
    assert "pull/1" in out


async def test_post_missing_title_returns_400(monkeypatch) -> None:
    _stub_repo(monkeypatch)
    token = sign_compose_token(bot_token=_BOT, window_id="@5")
    async with _ComposeServer() as client:
        async with client.get(f"/compose/{token}") as resp:
            body = await resp.text()
        nonce = re.search(r'name="csrf" value="([^"]+)"', body).group(1)
        async with client.post(
            f"/compose/{token}/pr", data={"title": "", "csrf": nonce}
        ) as resp:
            assert resp.status == 400


def test_state_module_unused_import_guard() -> None:
    # `state` import is exercised by _resolve_repo in production; keep it linked.
    assert state.WindowSidecar is not None

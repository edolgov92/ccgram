from __future__ import annotations

import subprocess
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from ccgram.miniapp.server import build_app
from ccgram_pro.git_ops import capture_snapshot
from ccgram_pro.share.tokens import sign_share_token
from ccgram_pro.web import register_diff_routes

_BOT = "12345:test-bot-token-aaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _init_repo(root: Path) -> Path:
    repo = root / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("line one\nline two\nline three\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _seed(window_id: str, tmp_path: Path, *, change: bool) -> Path:
    repo = _init_repo(tmp_path)
    capture_snapshot(window_id=window_id, project_root=repo)  # n0
    if change:
        (repo / "feature.py").write_text("def added():\n    return 1\n")
    capture_snapshot(window_id=window_id, project_root=repo)  # n1
    return repo


class _DiffServer:
    async def __aenter__(self):
        app = build_app(bot_token=_BOT)
        register_diff_routes(app)
        self.server = TestServer(app)
        self.client = TestClient(self.server)
        await self.client.start_server()
        return self.client

    async def __aexit__(self, *exc):
        await self.client.close()


async def test_diff_page_default_iteration(tmp_path: Path) -> None:
    _seed("@d1", tmp_path, change=True)
    token = sign_share_token(bot_token=_BOT, share_id="@d1")
    async with _DiffServer() as client, client.get(f"/diff/{token}") as resp:
        assert resp.status == 200
        body = await resp.text()
    assert "feature.py" in body
    assert "Last iteration" in body and "Since session start" in body


async def test_diff_page_session_anchor(tmp_path: Path) -> None:
    _seed("@d2", tmp_path, change=True)
    token = sign_share_token(bot_token=_BOT, share_id="@d2")
    async with _DiffServer() as client:
        async with client.get(f"/diff/{token}?anchor=session") as resp:
            assert resp.status == 200
            body = await resp.text()
    assert "feature.py" in body


async def test_diff_session_anchor_excludes_other_branch(tmp_path: Path) -> None:
    # Session starts on a branch carrying only_on_a.txt, switches branches and
    # makes one edit. "Since session start" must show only the edit.
    repo = _init_repo(tmp_path)
    (repo / "only_on_a.txt").write_text("a-branch\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a-only work")
    capture_snapshot(window_id="@dbr", project_root=repo)  # n0
    _git(repo, "checkout", "-q", "-b", "feature/work")
    _git(repo, "rm", "-q", "only_on_a.txt")
    _git(repo, "commit", "-q", "-m", "drop a-only on feature")
    capture_snapshot(window_id="@dbr", project_root=repo)  # n1 first on feature
    (repo / "session_edit.py").write_text("x = 1\n")
    capture_snapshot(window_id="@dbr", project_root=repo)  # n2
    token = sign_share_token(bot_token=_BOT, share_id="@dbr")
    async with _DiffServer() as client:
        async with client.get(f"/diff/{token}?anchor=session") as resp:
            body = await resp.text()
    assert "session_edit.py" in body
    assert "only_on_a.txt" not in body


async def test_diff_empty_iteration_shows_empty_state(tmp_path: Path) -> None:
    _seed("@d3", tmp_path, change=False)  # n1 == n0, no change
    token = sign_share_token(bot_token=_BOT, share_id="@d3")
    async with _DiffServer() as client, client.get(f"/diff/{token}") as resp:
        body = await resp.text()
    assert "No code changes" in body


async def test_diff_invalid_token_403(tmp_path: Path) -> None:
    async with _DiffServer() as client, client.get("/diff/garbage") as resp:
        assert resp.status == 403


async def test_diff_no_snapshots_404(tmp_path: Path) -> None:
    token = sign_share_token(bot_token=_BOT, share_id="@never")
    async with _DiffServer() as client, client.get(f"/diff/{token}") as resp:
        assert resp.status == 404


async def test_expand_returns_context_json(tmp_path: Path) -> None:
    _seed("@d4", tmp_path, change=True)
    token = sign_share_token(bot_token=_BOT, share_id="@d4")
    async with _DiffServer() as client:
        async with client.get(
            f"/diff/{token}/expand?anchor=session&path=README.md&start=1&count=2"
        ) as resp:
            assert resp.status == 200
            data = await resp.json()
    assert data["lines"][:2] == ["line one", "line two"]


async def test_expand_rejects_path_traversal(tmp_path: Path) -> None:
    _seed("@d5", tmp_path, change=True)
    token = sign_share_token(bot_token=_BOT, share_id="@d5")
    async with _DiffServer() as client:
        async with client.get(
            f"/diff/{token}/expand?anchor=session&path=../etc/passwd&start=1&count=2"
        ) as resp:
            assert resp.status == 400


async def test_expand_bad_token_403(tmp_path: Path) -> None:
    async with _DiffServer() as client:
        async with client.get("/diff/garbage/expand?path=x&start=1&count=2") as resp:
            assert resp.status == 403

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ccgram_pro.output_pipeline import summarizer

_BOT = "12345:test-bot-token-aaaaaaaaaaaaaaaaaaaaaaaaaaa"


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._next_id = 1000

    async def send_message(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)


async def test_post_summary_attaches_view_diff_and_settings_buttons() -> None:
    client = _StubClient()
    await summarizer._post_summary_message(
        client=client,
        chat_id=1,
        thread_id=2,
        summary="done",
        link_url="https://x/view/t",
        diff_url="https://x/diff/t",
        window_id="@5",
    )
    assert len(client.calls) == 1
    markup = client.calls[0]["reply_markup"]
    flat = [b for row in markup.inline_keyboard for b in row]
    urls = [b.url for b in flat if b.url]
    cbs = [b.callback_data for b in flat if b.callback_data]
    assert "https://x/view/t" in urls
    assert "https://x/diff/t" in urls
    assert any(c.startswith("ccgrampro:set:open:@5") for c in cbs)


async def test_post_summary_no_settings_without_window_id() -> None:
    client = _StubClient()
    await summarizer._post_summary_message(
        client=client,
        chat_id=1,
        thread_id=2,
        summary="done",
        link_url="https://x/view/t",
        diff_url=None,
        window_id=None,
    )
    markup = client.calls[0]["reply_markup"]
    cbs = [
        b.callback_data
        for row in markup.inline_keyboard
        for b in row
        if b.callback_data
    ]
    assert cbs == []


async def test_post_summary_returns_last_chunk_id() -> None:
    client = _StubClient()
    result = await summarizer._post_summary_message(
        client=client,
        chat_id=1,
        thread_id=2,
        summary="short body",
        link_url="https://x/view/t",
        diff_url=None,
        window_id=None,
    )
    assert isinstance(result, int)
    assert result == client._next_id


async def test_full_summary_not_truncated() -> None:
    client = _StubClient()
    body = "A" * 1000
    await summarizer._post_summary_message(
        client=client,
        chat_id=1,
        thread_id=2,
        summary=body,
        link_url="https://x/view/t",
    )
    joined = "".join(c.get("text", "") for c in client.calls)
    assert body in joined
    assert "…" not in joined


async def test_long_summary_split_buttons_on_last_only() -> None:
    client = _StubClient()
    body = "\n".join("line " + str(i) + " " + "x" * 200 for i in range(60))
    assert len(body) > 4096
    await summarizer._post_summary_message(
        client=client,
        chat_id=1,
        thread_id=2,
        summary=body,
        link_url="https://x/view/t",
        window_id="@5",
    )
    assert len(client.calls) >= 2
    assert all(c.get("reply_markup") is None for c in client.calls[:-1])
    assert client.calls[-1]["reply_markup"] is not None


async def test_strip_prior_summary_buttons_edits_each() -> None:
    edited: list[dict[str, Any]] = []

    class _Bot:
        async def edit_message_reply_markup(self, **kwargs: Any) -> None:
            edited.append(kwargs)

    entries = [
        {"chat_id": 1, "thread_id": 2, "message_id": 10},
        {"chat_id": 1, "thread_id": 3, "message_id": 11},
    ]
    await summarizer._strip_prior_summary_buttons(_Bot(), entries)
    assert [e["message_id"] for e in edited] == [10, 11]
    assert all(e["reply_markup"] is None for e in edited)


async def test_strip_prior_tolerates_edit_failure() -> None:
    from telegram.error import BadRequest

    class _Bot:
        async def edit_message_reply_markup(self, **kwargs: Any) -> None:
            raise BadRequest("message to edit not found")

    await summarizer._strip_prior_summary_buttons(
        _Bot(), [{"chat_id": 1, "thread_id": 2, "message_id": 10}]
    )


async def test_strip_prior_noop_when_bot_none() -> None:
    await summarizer._strip_prior_summary_buttons(
        None, [{"chat_id": 1, "thread_id": 2, "message_id": 10}]
    )


def _write_turn(path: Path, *, tldr: str | None, body: str) -> None:
    text = body
    if tldr is not None:
        text += f"\n<!--ccgram:tldr-->{tldr}<!--/ccgram:tldr-->"
    lines = [
        json.dumps({"type": "user", "message": {"content": "go"}}),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n")


def test_build_summary_prefers_tldr(tmp_path: Path) -> None:
    t = tmp_path / "t.jsonl"
    _write_turn(t, tldr="The complete summary.", body="lots of technical detail")
    summary_text, _share = summarizer._build_summary_and_share(
        transcript_path=str(t), window_id="@1", num_turns=1
    )
    assert summary_text == "The complete summary."


def test_build_summary_full_text_when_no_tldr(tmp_path: Path) -> None:
    t = tmp_path / "t.jsonl"
    _write_turn(t, tldr=None, body="Here is the full answer with no summary block.")
    summary_text, _share = summarizer._build_summary_and_share(
        transcript_path=str(t), window_id="@1", num_turns=1
    )
    assert "full answer" in summary_text


def test_build_summary_strips_progress_markers(tmp_path: Path) -> None:
    t = tmp_path / "t.jsonl"
    _write_turn(
        t,
        tldr=None,
        body="Answer <!--ccgram:progress-->Reading files<!--/ccgram:progress--> done",
    )
    summary_text, _share = summarizer._build_summary_and_share(
        transcript_path=str(t), window_id="@1", num_turns=1
    )
    assert "Reading files" not in summary_text
    assert "ccgram:progress" not in summary_text


def test_no_max_inline_constant() -> None:
    import inspect

    src = inspect.getsource(summarizer)
    assert "_MAX_INLINE_TEXT_CHARS" not in src


def test_no_llm_summarizer_reference() -> None:
    import inspect

    src = inspect.getsource(summarizer)
    assert "summarize_completion" not in src
    assert "_safe_llm_summary" not in src


def _init_git_repo(root: Path) -> Path:
    import subprocess

    repo = root / "r"
    repo.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@e.com"),
        ("config", "user.name", "T"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "i"],
        check=True,
        capture_output=True,
    )
    return repo


async def test_maybe_save_uses_workspace_path_over_cwd(tmp_path: Path) -> None:
    from ccgram_pro import state
    from ccgram_pro.git_ops import load_index

    repo = _init_git_repo(tmp_path)
    sidecar = state.WindowSidecar(window_id="@diff", window_creation_epoch=0.0)
    sidecar.workspace_path = str(repo)
    state.save(sidecar)
    await summarizer._maybe_save_diff_snapshots(window_id="@diff", bot_token=_BOT)
    index = load_index("@diff")
    assert index is not None
    assert index.project_root == str(repo)
    reloaded = state.load("@diff")
    assert reloaded is not None and reloaded.last_snapshot_id


async def test_maybe_save_returns_none_for_non_git(tmp_path: Path) -> None:
    from ccgram_pro import state
    from ccgram_pro.git_ops import load_index

    plain = tmp_path / "plain"
    plain.mkdir()
    sidecar = state.WindowSidecar(window_id="@nogit", window_creation_epoch=0.0)
    sidecar.workspace_path = str(plain)
    state.save(sidecar)
    result = await summarizer._maybe_save_diff_snapshots(
        window_id="@nogit", bot_token=_BOT
    )
    assert result is None
    assert load_index("@nogit") is None

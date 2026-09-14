from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from ccgram_pro import scenarios as scn
from telegram.ext import ApplicationHandlerStop


@pytest.fixture(autouse=True)
def _reset():
    scn._reset_for_testing()
    yield
    scn._reset_for_testing()


class _Msg:
    def __init__(
        self,
        *,
        chat_id: int = 10,
        msg_id: int = 500,
        thread_id: int = 2,
        text: str | None = None,
    ) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.message_id = msg_id
        self.message_thread_id = thread_id
        self.text = text
        self.edits: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []
        self.deleted = False

    async def edit_text(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)

    async def reply_text(self, **kwargs: Any) -> "_Msg":
        self.replies.append(kwargs)
        return _Msg(msg_id=999, thread_id=self.message_thread_id)

    async def delete(self) -> None:
        self.deleted = True


class _Query:
    def __init__(self, data: str, message: _Msg) -> None:
        self.data = data
        self.message = message
        self.answers: list[tuple[tuple, dict]] = []

    async def answer(self, *a: Any, **k: Any) -> None:
        self.answers.append((a, k))


class _Bot:
    def __init__(self) -> None:
        self.edits: list[dict[str, Any]] = []

    async def edit_message_text(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)


def _own(monkeypatch, val: bool = True) -> None:
    import ccgram.handlers.callback_helpers as ch

    monkeypatch.setattr(ch, "user_owns_window", lambda u, w: val)


def _stub_forward(monkeypatch) -> list[tuple[str, int, int, str]]:
    import ccgram_pro.input_pipeline.intercept as intercept

    forwarded: list[tuple[str, int, int, str]] = []

    async def _fwd(window_id, user_id, thread_id, text, client, anchor):  # noqa: ANN001
        forwarded.append((window_id, user_id, thread_id, text))

    monkeypatch.setattr(intercept, "_ORIGINAL_FORWARD_MESSAGE", _fwd)
    return forwarded


def _stub_bubble(monkeypatch) -> list[dict[str, Any]]:
    import ccgram_pro.input_pipeline.silencer_guard as sg
    import ccgram_pro.output_pipeline.progress_bubble as pb

    started: list[dict[str, Any]] = []

    async def _start(**k: Any) -> None:
        started.append(k)

    monkeypatch.setattr(pb, "start_bubble", _start)
    monkeypatch.setattr(sg, "is_silent_for_window", lambda wid: True)
    return started


def _detect(monkeypatch, repo: str | None) -> None:
    async def _d(_wid: str) -> str | None:
        return repo

    async def _ds(_wid: str) -> list[tuple[str, str]]:
        return [("/tmp/repo", repo)] if repo else []

    monkeypatch.setattr(scn, "_detect_pr_repo", _d)
    monkeypatch.setattr(scn, "_detect_pr_repos", _ds)


def _git_repo(monkeypatch, value: bool) -> None:
    async def _g(_wid: str) -> bool:
        return value

    monkeypatch.setattr(scn, "_is_git_repo", _g)


# ── codec / button ──────────────────────────────────────────────────────────


def test_codec_roundtrip() -> None:
    assert scn._decode(scn._encode("sr", "@5")) == ("sr", "@5")
    assert scn._decode(scn._encode("cp", "@5")) == ("cp", "@5")
    assert scn._decode(scn._encode("pr", "emdash-claude-main-x:@0")) == (
        "pr",
        "emdash-claude-main-x:@0",
    )
    assert scn._decode("garbage") is None
    assert scn._decode("ccgrampro:scn:bad:@5") is None
    assert scn._decode("ccgrampro:scn:menu:") is None


def test_button_icon_and_callback() -> None:
    b = scn.scenarios_button_for_window("sess:@9")
    assert b.text == "🎬"
    assert b.callback_data == "ccgrampro:scn:menu:sess:@9"
    assert len(b.callback_data.encode()) <= 64


# ── prompt content ────────────────────────────────────────────────────────────


def test_self_review_prompt_content() -> None:
    p = scn._SELF_REVIEW_PROMPT
    assert "code review" in p.lower()
    assert "корневую причину" in p
    assert "архитектур" in p.lower()
    assert p.rstrip().endswith(
        "Если ты обнаружишь какие-либо проблемы, исправь их перед завершением работы."
    )


def test_pr_fixer_prompt_substitution() -> None:
    p = scn._pr_fixer_prompt("4567", "frontend")
    assert "__PR__" not in p and "__REPO__" not in p
    assert "PR #4567" in p
    assert "REPO=frontend" in p
    assert "/root/projects/humanprogram/backend/var/pr-check.sh" in p
    assert "/Users/" not in p
    assert "не более 20 итераций" in p
    assert "pnpm typecheck" in p
    assert "afplay" not in p
    assert "Telegram" in p
    assert "reply-resolve" in p
    assert "status 4567" in p


# ── eligibility ─────────────────────────────────────────────────────────────


async def test_detect_pr_repo_backend(monkeypatch) -> None:
    monkeypatch.setattr(scn.state, "resolve_repo", lambda wid: "/repo")

    async def _url(_p: str) -> str:
        return "git@github-humanprogram:humanprogram/primer_server.git"

    monkeypatch.setattr(scn, "_git_remote_url", _url)
    assert await scn._detect_pr_repo("@5") == "backend"


async def test_detect_pr_repo_frontend(monkeypatch) -> None:
    monkeypatch.setattr(scn.state, "resolve_repo", lambda wid: "/repo")

    async def _url(_p: str) -> str:
        return "git@github-humanprogram:humanprogram/hyper_school_dashboard.git"

    monkeypatch.setattr(scn, "_git_remote_url", _url)
    assert await scn._detect_pr_repo("@5") == "frontend"


async def test_detect_pr_repo_none_for_other_remote(monkeypatch) -> None:
    monkeypatch.setattr(scn.state, "resolve_repo", lambda wid: "/repo")

    async def _url(_p: str) -> str:
        return "git@github.com:someone/other.git"

    monkeypatch.setattr(scn, "_git_remote_url", _url)
    assert await scn._detect_pr_repo("@5") is None


async def test_detect_pr_repo_none_without_repo(monkeypatch) -> None:
    monkeypatch.setattr(scn.state, "resolve_repo", lambda wid: None)
    assert await scn._detect_pr_repo("@5") is None


# ── menu ──────────────────────────────────────────────────────────────────────


def _callback_update(data: str, message: _Msg, *, user_id: int = 7) -> SimpleNamespace:
    return SimpleNamespace(
        callback_query=_Query(data, message),
        effective_user=SimpleNamespace(id=user_id),
        message=None,
    )


async def test_menu_shows_pr_when_eligible(monkeypatch) -> None:
    _own(monkeypatch)
    _detect(monkeypatch, "backend")
    _git_repo(monkeypatch, True)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:menu:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    kb = msg.replies[0]["reply_markup"]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "ccgrampro:scn:sr:@5" in cbs
    assert "ccgrampro:scn:pr:@5" in cbs


async def test_menu_hides_pr_when_ineligible(monkeypatch) -> None:
    _own(monkeypatch)
    _detect(monkeypatch, None)
    _git_repo(monkeypatch, False)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:menu:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    kb = msg.replies[0]["reply_markup"]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "ccgrampro:scn:sr:@5" in cbs
    assert "ccgrampro:scn:pr:@5" not in cbs


async def test_menu_shows_commit_push_for_git_repo(monkeypatch) -> None:
    _own(monkeypatch)
    _detect(monkeypatch, None)
    _git_repo(monkeypatch, True)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:menu:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    cbs = [
        b.callback_data
        for row in msg.replies[0]["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "ccgrampro:scn:cp:@5" in cbs


async def test_menu_hides_commit_push_for_non_git_dir(monkeypatch) -> None:
    _own(monkeypatch)
    _detect(monkeypatch, None)
    _git_repo(monkeypatch, False)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:menu:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    cbs = [
        b.callback_data
        for row in msg.replies[0]["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "ccgrampro:scn:cp:@5" not in cbs


async def test_commit_push_forwards_prompt(monkeypatch) -> None:
    _own(monkeypatch)
    forwarded = _stub_forward(monkeypatch)
    _stub_bubble(monkeypatch)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:cp:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    assert msg.edits and "Commit & push" in msg.edits[0]["text"]
    assert forwarded == [("@5", 7, 2, scn._COMMIT_PUSH_PROMPT)]


def test_commit_push_prompt_content() -> None:
    p = scn._COMMIT_PUSH_PROMPT
    low = p.lower()
    # No Claude co-authoring / AI attribution in the commit message.
    assert "co-authored-by" in low
    assert "do not add" in low or "don't add" in low
    assert "claude" in low
    # Selective staging (not add-all), meaningful message, push.
    assert "git add -A" in p
    assert "commit message" in low
    assert "push" in low


async def test_menu_shows_sync_main_for_git_repo(monkeypatch) -> None:
    _own(monkeypatch)
    _detect(monkeypatch, None)
    _git_repo(monkeypatch, True)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:menu:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    cbs = [
        b.callback_data
        for row in msg.replies[0]["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "ccgrampro:scn:sm:@5" in cbs


async def test_menu_hides_sync_main_for_non_git_dir(monkeypatch) -> None:
    _own(monkeypatch)
    _detect(monkeypatch, None)
    _git_repo(monkeypatch, False)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:menu:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    cbs = [
        b.callback_data
        for row in msg.replies[0]["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "ccgrampro:scn:sm:@5" not in cbs


async def test_sync_main_forwards_prompt(monkeypatch) -> None:
    _own(monkeypatch)
    forwarded = _stub_forward(monkeypatch)
    _stub_bubble(monkeypatch)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:sm:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    assert msg.edits and "Sync main branch" in msg.edits[0]["text"]
    assert forwarded == [("@5", 7, 2, scn._SYNC_MAIN_PROMPT)]


def test_sync_main_prompt_content() -> None:
    p = scn._SYNC_MAIN_PROMPT
    low = p.lower()
    # Detects the real default branch (develop for backend, not just "main").
    assert "develop" in low
    assert "remote show origin" in low or "symbolic-ref" in low
    # Safe: fast-forward only, never clobber uncommitted work.
    assert "--ff-only" in p
    assert "stop" in low and ("dirty" in low or "uncommitted" in low)
    assert "do not stash" in low or "not stash" in low
    # Codec accepts the new action.
    assert scn._decode(scn._encode("sm", "@5")) == ("sm", "@5")


async def test_menu_rejects_foreign_user(monkeypatch) -> None:
    _own(monkeypatch, val=False)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:menu:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    assert not msg.replies
    assert any(
        "not your session" in str(a).lower() for a, _k in update.callback_query.answers
    )


async def _menu_cbs_for_remote(monkeypatch, remote_url: str | None) -> list[str]:
    """Open the menu exercising the REAL _detect_pr_repo over *remote_url*."""
    _own(monkeypatch)
    monkeypatch.setattr(scn.state, "resolve_repo", lambda wid: "/repo")

    async def _url(_p: str) -> str | None:
        return remote_url

    monkeypatch.setattr(scn, "_git_remote_url", _url)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:menu:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    kb = msg.replies[0]["reply_markup"]
    return [b.callback_data for row in kb.inline_keyboard for b in row]


async def test_menu_hides_pr_for_unrelated_repo(monkeypatch) -> None:
    cbs = await _menu_cbs_for_remote(monkeypatch, "git@github.com:someone/other.git")
    assert "ccgrampro:scn:sr:@5" in cbs
    assert "ccgrampro:scn:pr:@5" not in cbs


async def test_menu_hides_pr_for_same_name_different_owner(monkeypatch) -> None:
    # A repo that merely shares a name under a different owner must NOT match.
    cbs = await _menu_cbs_for_remote(
        monkeypatch, "git@github.com:someoneelse/primer_server.git"
    )
    assert "ccgrampro:scn:pr:@5" not in cbs


async def test_menu_hides_pr_when_no_remote(monkeypatch) -> None:
    cbs = await _menu_cbs_for_remote(monkeypatch, None)
    assert "ccgrampro:scn:pr:@5" not in cbs


async def test_menu_shows_pr_for_humanprogram_backend(monkeypatch) -> None:
    cbs = await _menu_cbs_for_remote(
        monkeypatch, "git@github-humanprogram:humanprogram/primer_server.git"
    )
    assert "ccgrampro:scn:pr:@5" in cbs


async def test_menu_shows_pr_for_humanprogram_app(monkeypatch) -> None:
    cbs = await _menu_cbs_for_remote(
        monkeypatch, "https://github.com/humanprogram/hyper_school_dashboard.git"
    )
    assert "ccgrampro:scn:pr:@5" in cbs


async def test_ask_pr_number_rejected_for_unrelated_repo(monkeypatch) -> None:
    _own(monkeypatch)
    monkeypatch.setattr(scn.state, "resolve_repo", lambda wid: "/repo")

    async def _url(_p: str) -> str:
        return "git@github.com:someone/other.git"

    monkeypatch.setattr(scn, "_git_remote_url", _url)
    msg = _Msg()
    ctx = SimpleNamespace(bot=_Bot(), user_data={})
    update = _callback_update("ccgrampro:scn:pr:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, ctx)
    assert scn.AWAITING_PR_NUMBER not in ctx.user_data
    assert any(
        "humanprogram" in str(a).lower() for a, _k in update.callback_query.answers
    )


# ── self-review ─────────────────────────────────────────────────────────────


async def test_self_review_forwards_and_records(monkeypatch) -> None:
    _own(monkeypatch)
    forwarded = _stub_forward(monkeypatch)
    started = _stub_bubble(monkeypatch)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:sr:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    assert msg.edits and "Self-review" in msg.edits[0]["text"]
    assert msg.edits[0]["reply_markup"] is None
    assert forwarded == [("@5", 7, 2, scn._SELF_REVIEW_PROMPT)]
    assert started and started[0]["window_id"] == "@5"


# ── PR auto-fixer ────────────────────────────────────────────────────────────


async def test_ask_pr_number_arms_flag(monkeypatch) -> None:
    _own(monkeypatch)
    _detect(monkeypatch, "backend")
    msg = _Msg()
    ctx = SimpleNamespace(bot=_Bot(), user_data={})
    update = _callback_update("ccgrampro:scn:pr:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, ctx)
    pend = ctx.user_data[scn.AWAITING_PR_NUMBER]
    assert pend == {
        "chat_id": 10,
        "thread_id": 2,
        "window_id": "@5",
        "repo": "backend",
        "targets": [["/tmp/repo", "backend"]],
        "prompt_msg_id": 500,
        "user_id": 7,
    }
    assert msg.edits and "PR number" in msg.edits[0]["text"]


async def test_consume_valid_number_runs_pr_fixer(monkeypatch) -> None:
    forwarded = _stub_forward(monkeypatch)
    _stub_bubble(monkeypatch)
    bot = _Bot()
    ctx = SimpleNamespace(
        bot=bot,
        user_data={
            scn.AWAITING_PR_NUMBER: {
                "chat_id": 10,
                "thread_id": 2,
                "window_id": "@5",
                "repo": "backend",
                "prompt_msg_id": 500,
                "user_id": 7,
            }
        },
    )
    message = _Msg(msg_id=600, thread_id=2, text="#1234")
    update = SimpleNamespace(
        message=message, callback_query=None, effective_user=SimpleNamespace(id=7)
    )
    with pytest.raises(ApplicationHandlerStop):
        await scn.consume_pr_number_reply(update, ctx)
    assert scn.AWAITING_PR_NUMBER not in ctx.user_data
    assert len(forwarded) == 1
    sent_prompt = forwarded[0][3]
    assert "PR #1234" in sent_prompt and "REPO=backend" in sent_prompt
    assert bot.edits and "PR #1234" in bot.edits[0]["text"]
    assert message.deleted


async def test_consume_invalid_number_keeps_armed(monkeypatch) -> None:
    forwarded = _stub_forward(monkeypatch)
    bot = _Bot()
    ctx = SimpleNamespace(
        bot=bot,
        user_data={
            scn.AWAITING_PR_NUMBER: {
                "chat_id": 10,
                "thread_id": 2,
                "window_id": "@5",
                "repo": "backend",
                "prompt_msg_id": 500,
                "user_id": 7,
            }
        },
    )
    message = _Msg(msg_id=600, thread_id=2, text="not a number")
    update = SimpleNamespace(
        message=message, callback_query=None, effective_user=SimpleNamespace(id=7)
    )
    with pytest.raises(ApplicationHandlerStop):
        await scn.consume_pr_number_reply(update, ctx)
    assert scn.AWAITING_PR_NUMBER in ctx.user_data
    assert forwarded == []
    assert bot.edits and "doesn't look like a PR number" in bot.edits[0]["text"]
    assert message.deleted


async def test_consume_passthrough_when_not_armed(monkeypatch) -> None:
    forwarded = _stub_forward(monkeypatch)
    message = _Msg(text="hello")
    update = SimpleNamespace(message=message, callback_query=None)
    await scn.consume_pr_number_reply(update, SimpleNamespace(user_data={}))
    assert forwarded == []
    assert not message.deleted


async def test_consume_wrong_thread_passthrough(monkeypatch) -> None:
    forwarded = _stub_forward(monkeypatch)
    ctx = SimpleNamespace(
        user_data={
            scn.AWAITING_PR_NUMBER: {
                "chat_id": 10,
                "thread_id": 2,
                "window_id": "@5",
                "repo": "backend",
                "prompt_msg_id": 500,
                "user_id": 7,
            }
        }
    )
    message = _Msg(msg_id=600, thread_id=9, text="1234")
    update = SimpleNamespace(message=message, callback_query=None)
    await scn.consume_pr_number_reply(update, ctx)
    assert scn.AWAITING_PR_NUMBER in ctx.user_data
    assert forwarded == []


async def test_cancel_clears_flag_and_deletes(monkeypatch) -> None:
    _own(monkeypatch)
    msg = _Msg()
    ctx = SimpleNamespace(
        bot=_Bot(), user_data={scn.AWAITING_PR_NUMBER: {"window_id": "@5"}}
    )
    update = _callback_update("ccgrampro:scn:x:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, ctx)
    assert scn.AWAITING_PR_NUMBER not in ctx.user_data
    assert msg.deleted


def test_decode_feature_branch_and_full_flow() -> None:
    assert scn._decode(scn._encode("fb", "@5")) == ("fb", "@5")
    assert scn._decode(scn._encode("fa", "@5")) == ("fa", "@5")


async def test_menu_shows_feature_branch_for_git_repo(monkeypatch) -> None:
    _own(monkeypatch)
    _detect(monkeypatch, None)
    _git_repo(monkeypatch, True)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:menu:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    cbs = [
        b.callback_data
        for row in msg.replies[0]["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "ccgrampro:scn:fb:@5" in cbs


async def test_menu_hides_feature_branch_for_non_git_dir(monkeypatch) -> None:
    _own(monkeypatch)
    _detect(monkeypatch, None)
    _git_repo(monkeypatch, False)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:menu:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    cbs = [
        b.callback_data
        for row in msg.replies[0]["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "ccgrampro:scn:fb:@5" not in cbs


async def test_menu_shows_full_flow_when_pr_eligible(monkeypatch) -> None:
    _own(monkeypatch)
    _detect(monkeypatch, "backend")
    _git_repo(monkeypatch, True)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:menu:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    cbs = [
        b.callback_data
        for row in msg.replies[0]["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "ccgrampro:scn:fa:@5" in cbs


async def test_menu_hides_full_flow_when_ineligible(monkeypatch) -> None:
    _own(monkeypatch)
    _detect(monkeypatch, None)
    _git_repo(monkeypatch, True)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:menu:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    cbs = [
        b.callback_data
        for row in msg.replies[0]["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "ccgrampro:scn:fa:@5" not in cbs


async def test_feature_branch_forwards_prompt(monkeypatch) -> None:
    _own(monkeypatch)
    forwarded = _stub_forward(monkeypatch)
    _stub_bubble(monkeypatch)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:fb:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    assert msg.edits and "Feature branch" in msg.edits[0]["text"]
    assert forwarded == [("@5", 7, 2, scn._FEATURE_BRANCH_PROMPT)]


async def test_full_flow_forwards_prompt(monkeypatch) -> None:
    _own(monkeypatch)
    _detect(monkeypatch, "backend")
    forwarded = _stub_forward(monkeypatch)
    _stub_bubble(monkeypatch)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:fa:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    assert msg.edits and "auto-fix" in msg.edits[0]["text"]
    assert forwarded == [("@5", 7, 2, scn._full_flow_prompt("backend"))]


async def test_full_flow_ineligible_does_not_forward(monkeypatch) -> None:
    _own(monkeypatch)
    _detect(monkeypatch, None)
    forwarded = _stub_forward(monkeypatch)
    _stub_bubble(monkeypatch)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:fa:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    assert forwarded == []


def test_feature_branch_prompt_content() -> None:
    p = scn._FEATURE_BRANCH_PROMPT
    low = p.lower()
    assert "feature branch" in low or "feature/" in p
    assert "co-authored-by" in low
    assert "claude" in low
    assert "git add -A" in p
    assert "push" in low


def test_full_flow_prompt_content() -> None:
    p = scn._full_flow_prompt("backend")
    assert "__PR__" not in p and "__REPO__" not in p
    assert "STEP 1" in p and "STEP 2" in p and "STEP 3" in p
    assert "gh pr create" in p
    assert "REPO=backend" in p
    assert "<PR>" in p
    assert "не более 20 итераций" in p
    assert "claude" in p.lower()
    assert "develop" in p


# ── manual testing ───────────────────────────────────────────────────────────


def test_decode_manual_testing() -> None:
    assert scn._decode(scn._encode("mt", "@5")) == ("mt", "@5")


def _menu_callbacks(msg: _Msg) -> list[str]:
    kb = msg.replies[0]["reply_markup"]
    return [b.callback_data for row in kb.inline_keyboard for b in row]


async def test_menu_shows_manual_testing_when_pr_eligible(monkeypatch) -> None:
    _own(monkeypatch)
    _git_repo(monkeypatch, True)
    _detect(monkeypatch, "backend")
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:menu:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    assert "ccgrampro:scn:mt:@5" in _menu_callbacks(msg)


async def test_menu_hides_manual_testing_when_ineligible(monkeypatch) -> None:
    _own(monkeypatch)
    _git_repo(monkeypatch, True)
    _detect(monkeypatch, None)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:menu:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    assert "ccgrampro:scn:mt:@5" not in _menu_callbacks(msg)


async def test_manual_testing_forwards_prompt(monkeypatch) -> None:
    _own(monkeypatch)
    _detect(monkeypatch, "frontend")
    forwarded = _stub_forward(monkeypatch)
    _stub_bubble(monkeypatch)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:mt:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    assert msg.edits and "Manual testing" in msg.edits[0]["text"]
    assert forwarded == [("@5", 7, 2, scn._MANUAL_TESTING_PROMPT)]


async def test_manual_testing_ineligible_does_not_forward(monkeypatch) -> None:
    _own(monkeypatch)
    _detect(monkeypatch, None)
    forwarded = _stub_forward(monkeypatch)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:mt:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    assert forwarded == []


def test_manual_testing_prompt_content() -> None:
    p = scn._MANUAL_TESTING_PROMPT
    # VPS paths, never the author's laptop paths.
    assert "/root/projects/humanprogram/backend" in p
    assert "/root/projects/humanprogram/app" in p
    assert "/Users/" not in p and "~/Documents" not in p
    # Server adaptations: headless Chrome, explicit dashboard .env, evidence dir.
    assert "MT_HEADLESS=1" in p
    assert "MT_CHROME=" in p
    assert "MT_DASHBOARD_ENV=/root/projects/humanprogram/app/.env" in p
    assert "/root/projects/humanprogram/.mt-evidence/" in p
    # Preflight + prod-replica guard + artifact link contract.
    assert "primer_development" in p
    assert "hp-db" in p and "только для чтения" in p
    assert "Artifact" in p and "TL;DR" in p
    assert "labels" in p


# ── full-stack (composite) sessions ──────────────────────────────────────────


def _composite_sidecar(window_id: str = "@5") -> None:
    from ccgram_pro import state
    from ccgram_pro.config import ensure_layer_dirs

    ensure_layer_dirs()
    sidecar = state.WindowSidecar(window_id=window_id, window_creation_epoch=1.0)
    sidecar.project_repos = ["/srv/hp/backend", "/srv/hp/app"]
    state.save(sidecar)


def _stub_run_git(monkeypatch, remotes: dict[str, str | None], git: set[str]) -> None:
    async def _fake(repo_path, *args):  # noqa: ANN001
        if args == ("rev-parse", "--is-inside-work-tree"):
            return "true" if repo_path in git else None
        if args == ("remote", "get-url", "origin"):
            return remotes.get(repo_path)
        return None

    monkeypatch.setattr(scn, "_run_git", _fake)


def test_session_repo_paths_prefers_sidecar_composite(monkeypatch) -> None:
    _composite_sidecar()
    assert scn._session_repo_paths("@5") == ["/srv/hp/backend", "/srv/hp/app"]
    assert scn._is_composite("@5") is True


def test_session_repo_paths_falls_back_to_resolved_cwd(monkeypatch) -> None:
    monkeypatch.setattr(scn.state, "resolve_repo", lambda wid: "/srv/single")
    assert scn._session_repo_paths("@7") == ["/srv/single"]
    assert scn._is_composite("@7") is False


async def test_is_git_repo_requires_every_composite_repo(monkeypatch) -> None:
    _composite_sidecar()
    _stub_run_git(monkeypatch, {}, {"/srv/hp/backend", "/srv/hp/app"})
    assert await scn._is_git_repo("@5") is True
    _stub_run_git(monkeypatch, {}, {"/srv/hp/backend"})
    assert await scn._is_git_repo("@5") is False


async def test_detect_pr_repos_composite_returns_both(monkeypatch) -> None:
    _composite_sidecar()
    _stub_run_git(
        monkeypatch,
        {
            "/srv/hp/backend": "git@github-humanprogram:humanprogram/primer_server.git",
            "/srv/hp/app": "git@github-humanprogram:humanprogram/hyper_school_dashboard.git",
        },
        set(),
    )
    assert await scn._detect_pr_repos("@5") == [
        ("/srv/hp/backend", "backend"),
        ("/srv/hp/app", "frontend"),
    ]
    assert await scn._detect_pr_repo("@5") == "backend"


def test_scope_preamble_empty_for_single_repo(monkeypatch) -> None:
    monkeypatch.setattr(scn.state, "resolve_repo", lambda wid: "/srv/single")
    assert scn._scope_preamble("@7", ru=True) == ""
    assert scn._scope_preamble("@7", ru=False) == ""


def test_scope_preamble_lists_repos_in_both_languages() -> None:
    _composite_sidecar()
    ru = scn._scope_preamble("@5", ru=True)
    en = scn._scope_preamble("@5", ru=False)
    for text in (ru, en):
        assert "/srv/hp/backend" in text and "/srv/hp/app" in text
        assert "git -C" in text
    assert "full-stack" in ru and "КАЖДОГО" in ru
    assert "EACH" in en


async def test_self_review_composite_gets_scope_preamble(monkeypatch) -> None:
    _own(monkeypatch)
    _composite_sidecar()
    forwarded = _stub_forward(monkeypatch)
    _stub_bubble(monkeypatch)
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:sr:@5", msg)
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(update, SimpleNamespace(bot=_Bot()))
    prompt = forwarded[0][3]
    assert prompt.startswith("Это full-stack сессия")
    assert prompt.endswith(scn._SELF_REVIEW_PROMPT)


@pytest.mark.parametrize(
    ("text", "expected", "result"),
    [
        ("1234", 1, ["1234"]),
        ("#1234", 1, ["1234"]),
        ("abc", 1, None),
        ("12 34", 1, None),
        ("1234 567", 2, ["1234", "567"]),
        ("1234 -", 2, ["1234", None]),
        ("- 567", 2, [None, "567"]),
        ("- -", 2, None),
        ("1234", 2, None),
        ("1234 x", 2, None),
    ],
)
def test_parse_pr_reply(text: str, expected: int, result) -> None:
    assert scn._parse_pr_reply(text, expected) == result


def test_pr_fixer_prompt_multi_lists_targets_and_keeps_rules() -> None:
    p = scn._pr_fixer_prompt_multi(
        [("/srv/hp/backend", "backend", "1234"), ("/srv/hp/app", "frontend", "567")]
    )
    assert "__PR__" not in p and "__REPO__" not in p
    assert "backend: /srv/hp/backend — REPO=backend, PR #1234" in p
    assert "frontend: /srv/hp/app — REPO=frontend, PR #567" in p
    assert "/root/projects/humanprogram/backend/var/pr-check.sh" in p
    assert "не более 20 итераций" in p
    assert "afplay" not in p


def test_full_flow_prompt_multi_content() -> None:
    p = scn._full_flow_prompt_multi(
        [("/srv/hp/backend", "backend"), ("/srv/hp/app", "frontend")]
    )
    assert "__PR__" not in p and "__REPO__" not in p
    assert "STEP 1" in p and "STEP 2" in p and "STEP 3" in p
    assert "КАЖДОМ репозитории" in p
    assert "backend → `develop`" in p and "frontend → `main`" in p
    assert "gh pr create" in p
    assert "REPO=backend" in p and "REPO=frontend" in p
    assert "<PR>" in p


async def test_ask_pr_number_composite_asks_for_two_numbers(monkeypatch) -> None:
    _own(monkeypatch)
    _composite_sidecar()
    _stub_run_git(
        monkeypatch,
        {
            "/srv/hp/backend": "git@github-humanprogram:humanprogram/primer_server.git",
            "/srv/hp/app": "git@github-humanprogram:humanprogram/hyper_school_dashboard.git",
        },
        set(),
    )
    msg = _Msg()
    update = _callback_update("ccgrampro:scn:pr:@5", msg)
    user_data: dict[str, Any] = {}
    with pytest.raises(ApplicationHandlerStop):
        await scn.handle_scenarios_callback(
            update, SimpleNamespace(bot=_Bot(), user_data=user_data)
        )
    pend = user_data[scn.AWAITING_PR_NUMBER]
    assert [tuple(t) for t in pend["targets"]] == [
        ("/srv/hp/backend", "backend"),
        ("/srv/hp/app", "frontend"),
    ]
    assert "full-stack" in msg.edits[0]["text"]
    assert "backend frontend" in msg.edits[0]["text"]


async def test_consume_composite_numbers_runs_multi_fixer(monkeypatch) -> None:
    forwarded = _stub_forward(monkeypatch)
    _stub_bubble(monkeypatch)
    import ccgram.handlers.callback_helpers as ch

    monkeypatch.setattr(ch, "get_thread_id", lambda update: 2)
    pend = {
        "chat_id": 10,
        "thread_id": 2,
        "window_id": "@5",
        "repo": "backend",
        "targets": [["/srv/hp/backend", "backend"], ["/srv/hp/app", "frontend"]],
        "prompt_msg_id": 500,
        "user_id": 7,
    }
    user_data: dict[str, Any] = {scn.AWAITING_PR_NUMBER: pend}
    bot = _Bot()
    msg = _Msg(text="1234 -")
    update = SimpleNamespace(message=msg, effective_user=SimpleNamespace(id=7))
    with pytest.raises(ApplicationHandlerStop):
        await scn.consume_pr_number_reply(
            update, SimpleNamespace(bot=bot, user_data=user_data)
        )
    assert scn.AWAITING_PR_NUMBER not in user_data
    prompt = forwarded[0][3]
    assert "backend: /srv/hp/backend — REPO=backend, PR #1234" in prompt
    assert "frontend" not in prompt.split("Прогони цикл ниже")[0]
    assert "#1234 (backend)" in bot.edits[0]["text"]
    assert msg.deleted is True

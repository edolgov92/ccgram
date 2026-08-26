import asyncio
from types import SimpleNamespace


import pytest

from ccgram.handlers import auth_recovery
from ccgram.handlers.auth_recovery import (
    LOGIN_CODE_RE,
    classify_login_screen,
    consume_login_code,
    extract_oauth_url,
    is_auth_failure,
    maybe_start_login_flow,
)


@pytest.fixture(autouse=True)
def _reset():
    auth_recovery._reset_for_testing()
    yield
    auth_recovery._reset_for_testing()


_TRUST_SCREEN = """
 Accessing workspace:
 /root
 Quick safety check: Is this a project you created or one you trust?
 ❯ 1. Yes, I trust this folder
   2. No, exit
 Enter to confirm · Esc to cancel
"""

_METHOD_SCREEN = """
   Login
   Claude Code can be used with your Claude subscription or billed based on
   API usage through your Console account.
   Select login method:
   ❯ 1. Claude account with subscription · Pro, Max, Team, or Enterprise
     2. Anthropic Console account · API usage billing
"""

_URL_SCREEN = """
   Login
   Browser didn't open? Use the url below to sign in (c to copy)
https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88
ed-5944d1962f5e&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.co
m%2Foauth%2Fcode%2Fcallback&scope=org%3Acreate_api_key+user%3Aprofile&code_
challenge=mnJO41z3JcbkY16AW_QDw2PZ1xhonJKvegAaGpL8yA8&code_challenge_method=S256
&state=E_dbqxXnxSsG7rvfwUfgnUktAMdDhkyiqk7j9D3sUxE
   Hold Shift (Option in iTerm2, Fn in Terminal.app) while selecting to use
   Paste code here if prompted >
"""

_SUCCESS_SCREEN = """
   Login
   Logged in as edolgov@outlook.com
   Login successful. Press Enter to continue…
"""

_PROMPT_SCREEN = """
 ▐▛███▛█   Claude Code v2.1.246
❯
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
"""

_REAL_CODE = (
    "C5Hujp7LV9oNDZVmQIDViIW6pqejRS0I9qQlHJSXbbG64Iqe"
    "#E_dbqxXnxSsG7rvfwUfgnUktAMdDhkyiqk7j9D3sUxE"
)


class TestIsAuthFailure:
    @pytest.mark.parametrize(
        ("error", "details", "expected"),
        [
            ("authentication_failed", "", True),
            ("Authentication error", "token revoked", True),
            ("api_error", "401 unauthorized", True),
            ("invalid api key", "", True),
            ("OAuth token has expired", "", True),
            ("overloaded_error", "", False),
            ("rate_limit_error", "", False),
            ("unknown", "", False),
        ],
    )
    def test_variants(self, error: str, details: str, expected: bool) -> None:
        assert is_auth_failure(error, details) is expected


class TestClassifyLoginScreen:
    def test_screens(self) -> None:
        assert classify_login_screen(_TRUST_SCREEN) == "trust"
        assert classify_login_screen(_METHOD_SCREEN) == "method_select"
        assert classify_login_screen(_URL_SCREEN) == "url_shown"
        assert classify_login_screen(_SUCCESS_SCREEN) == "success"
        assert classify_login_screen(_PROMPT_SCREEN) == "prompt_ready"
        assert classify_login_screen("garbage") == "unknown"


class TestExtractOauthUrl:
    def test_joins_wrapped_lines(self) -> None:
        url = extract_oauth_url(_URL_SCREEN)
        assert url.startswith("https://claude.com/cai/oauth/authorize?code=true")
        assert "\n" not in url
        assert url.endswith("&state=E_dbqxXnxSsG7rvfwUfgnUktAMdDhkyiqk7j9D3sUxE")
        assert "client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e" in url

    def test_no_url(self) -> None:
        assert extract_oauth_url(_TRUST_SCREEN) == ""
        assert extract_oauth_url("https://claude.com/docs plain") == ""


class TestLoginCodeRe:
    def test_real_code_matches(self) -> None:
        assert LOGIN_CODE_RE.fullmatch(_REAL_CODE)

    @pytest.mark.parametrize(
        "text",
        [
            "привет, как дела?",
            "fix the bug in auth.py",
            "short#short",
            "has spaces " + _REAL_CODE,
            "no#hash-just-words-but-long-enough-to-be-suspicious",
        ],
    )
    def test_normal_text_does_not_match(self, text: str) -> None:
        assert LOGIN_CODE_RE.fullmatch(text) is None


def _make_flow(**overrides):
    flow = auth_recovery.LoginFlow(
        client=SimpleNamespace(),
        user_id=7,
        thread_id=100,
        chat_id=-1,
        started_at=0.0,
    )
    for key, value in overrides.items():
        setattr(flow, key, value)
    auth_recovery._flow = flow
    return flow


class TestConsumeLoginCode:
    async def test_no_flow_passes_through(self) -> None:
        message = SimpleNamespace()
        assert await consume_login_code(7, 100, _REAL_CODE, message) is False

    async def test_wrong_user_passes_through(self) -> None:
        _make_flow(awaiting_code=True)
        assert await consume_login_code(8, 100, _REAL_CODE, SimpleNamespace()) is False

    async def test_non_code_text_passes_through(self) -> None:
        _make_flow(awaiting_code=True)
        assert (
            await consume_login_code(7, 100, "обычный текст", SimpleNamespace())
            is False
        )

    async def test_code_consumed_and_event_set(self, monkeypatch) -> None:
        flow = _make_flow(awaiting_code=True)
        replies = []

        async def fake_reply(message, text):
            replies.append(text)

        monkeypatch.setattr(
            "ccgram.handlers.messaging_pipeline.message_sender.safe_reply", fake_reply
        )
        assert await consume_login_code(7, 999, f"  {_REAL_CODE}  ", SimpleNamespace())
        assert flow.code == _REAL_CODE
        assert flow.code_event.is_set()
        assert replies


class TestMaybeStartLoginFlow:
    async def test_disabled_by_config(self, monkeypatch) -> None:
        monkeypatch.setattr(auth_recovery.config, "auto_relogin", False)
        assert await maybe_start_login_flow(SimpleNamespace(), 7, 100, -1) is False

    async def test_starts_once_then_debounces(self, monkeypatch) -> None:
        monkeypatch.setattr(auth_recovery.config, "auto_relogin", True)
        started = []

        async def fake_run(flow):
            started.append(flow)

        monkeypatch.setattr(auth_recovery, "_run_login_flow", fake_run)
        assert await maybe_start_login_flow(SimpleNamespace(), 7, 100, -1) is True
        await asyncio.sleep(0)
        assert await maybe_start_login_flow(SimpleNamespace(), 7, 100, -1) is False
        assert len(started) == 1


class TestRunLoginFlow:
    async def test_full_roundtrip_success(self, monkeypatch) -> None:
        notifications = []

        async def fake_notify(flow, text):
            notifications.append(text)

        async def fake_drive():
            return "https://claude.com/cai/oauth/authorize?x=1"

        tmux_calls = []

        async def fake_tmux(*args):
            tmux_calls.append(args)
            return 0, ""

        async def fake_capture():
            return _SUCCESS_SCREEN

        monkeypatch.setattr(auth_recovery, "_notify", fake_notify)
        monkeypatch.setattr(auth_recovery, "_drive_login_to_url", fake_drive)
        monkeypatch.setattr(auth_recovery, "_run_tmux", fake_tmux)
        monkeypatch.setattr(auth_recovery, "_capture_login_pane", fake_capture)
        monkeypatch.setattr(auth_recovery, "_credentials_ok", lambda: True)

        flow = _make_flow()
        task = asyncio.create_task(auth_recovery._run_login_flow(flow))
        for _ in range(50):
            await asyncio.sleep(0)
            if flow.awaiting_code:
                break
        assert flow.awaiting_code
        assert any("claude.com" in n for n in notifications)

        flow.code = _REAL_CODE
        flow.code_event.set()
        await asyncio.wait_for(task, timeout=5)

        assert any("restored" in n for n in notifications)
        assert (
            "send-keys",
            "-t",
            auth_recovery.LOGIN_TMUX_SESSION,
            "-l",
            _REAL_CODE,
        ) in tmux_calls
        assert auth_recovery._flow is None

    async def test_drive_failure_notifies(self, monkeypatch) -> None:
        notifications = []

        async def fake_notify(flow, text):
            notifications.append(text)

        async def fake_drive():
            raise auth_recovery.LoginFlowError("boom")

        async def fake_tmux(*args):
            return 0, ""

        monkeypatch.setattr(auth_recovery, "_notify", fake_notify)
        monkeypatch.setattr(auth_recovery, "_drive_login_to_url", fake_drive)
        monkeypatch.setattr(auth_recovery, "_run_tmux", fake_tmux)

        flow = _make_flow()
        await auth_recovery._run_login_flow(flow)
        assert any("could not" in n for n in notifications)
        assert auth_recovery._flow is None

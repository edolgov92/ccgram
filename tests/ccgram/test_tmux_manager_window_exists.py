from unittest.mock import AsyncMock, patch

import pytest

from ccgram.tmux_manager import tmux_manager


class _Proc:
    def __init__(self, stdout: bytes, returncode: int) -> None:
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, b""


def _exec(stdout: bytes = b"", returncode: int = 0):
    return patch(
        "asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_Proc(stdout, returncode)),
    )


async def test_returns_true_when_window_listed() -> None:
    with _exec(b"@0\n@5\n@12\n"):
        assert await tmux_manager.window_exists("@5") is True


async def test_returns_false_only_on_successful_query_without_window() -> None:
    with _exec(b"@0\n@12\n"):
        assert await tmux_manager.window_exists("@5") is False


async def test_returns_none_when_tmux_fails() -> None:
    with _exec(b"", 1):
        assert await tmux_manager.window_exists("@5") is None


async def test_returns_none_on_os_error() -> None:
    with patch("asyncio.create_subprocess_exec", side_effect=OSError("boom")):
        assert await tmux_manager.window_exists("@5") is None


async def test_returns_none_on_timeout() -> None:
    class _Hang:
        returncode = None

        async def communicate(self):
            raise TimeoutError

        def kill(self):
            return None

        async def wait(self):
            return None

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_Hang())):
        assert await tmux_manager.window_exists("@5") is None


async def test_foreign_window_queries_its_own_session() -> None:
    captured: list[tuple] = []

    async def _spawn(*args, **kwargs):
        captured.append(args)
        return _Proc(b"@0\n", 0)

    with patch("asyncio.create_subprocess_exec", new=_spawn):
        assert await tmux_manager.window_exists("emdash-claude-main-abc:@0") is True
    assert "emdash-claude-main-abc" in captured[0]


@pytest.mark.parametrize("rc", [1, 2, 127])
async def test_any_nonzero_exit_is_unknown(rc: int) -> None:
    with _exec(b"", rc):
        assert await tmux_manager.window_exists("@5") is None

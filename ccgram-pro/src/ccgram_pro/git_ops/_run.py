"""Shared subprocess primitive — captures stdout + stderr, short timeouts."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_TIMEOUT_SECONDS = 30


class GitOpError(RuntimeError):
    """Raised when a git/gh invocation exits non-zero or errors."""

    def __init__(self, command: list[str], returncode: int, stderr: str):
        super().__init__(
            f"{' '.join(command[:3])} exited {returncode}: {stderr.strip() or '(no stderr)'}"
        )
        self.command = command
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class RunResult:
    stdout: str
    stderr: str
    returncode: int


def run_git(
    cwd: Path | str,
    *args: str,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
    check: bool = True,
) -> RunResult:
    """Run ``git -C <cwd> <args>``. Raises :class:`GitOpError` on non-zero."""
    cmd = ["git", "-C", str(cwd), *args]
    return _run(cmd, timeout=timeout, check=check)


def run_gh(
    *args: str,
    cwd: Path | str | None = None,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
    check: bool = True,
) -> RunResult:
    """Run ``gh <args>`` with optional cwd."""
    cmd = ["gh", *args]
    return _run(cmd, cwd=cwd, timeout=timeout, check=check)


def _run(
    cmd: list[str],
    *,
    cwd: Path | str | None = None,
    timeout: int,
    check: bool,
) -> RunResult:
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitOpError(cmd, 127, str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitOpError(cmd, 124, f"timed out after {timeout}s") from exc
    result = RunResult(
        stdout=completed.stdout,
        stderr=completed.stderr,
        returncode=completed.returncode,
    )
    if check and completed.returncode != 0:
        raise GitOpError(cmd, completed.returncode, completed.stderr)
    return result

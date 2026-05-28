"""Auto-detect and run the workspace's package install command.

Detection looks at the lock/manifest files present in the workspace root.
Order matters — pnpm-lock beats package-lock so a hybrid repo picks the
faster tool, ``uv.lock`` beats bare ``pyproject.toml`` so we prefer uv
when the project committed an explicit lock.

A user-supplied ``install_command`` on the matching :class:`Project`
overrides the auto-detection. The empty string ``""`` is a sentinel
meaning "skip install entirely" — useful when the workspace ships
vendored deps or the user wants to wire up their own bootstrap.
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from pathlib import Path

import structlog

from .paths import install_log_path

logger = structlog.get_logger()


@dataclass(frozen=True)
class InstallResult:
    """Outcome of an install run. Captured for the manager + doctor."""

    command: str
    returncode: int
    duration_seconds: float
    log_path: Path

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0


# Detection rules: ordered list of (filename, default command). First hit wins.
# Tuples are tried in declaration order, so favor lockfiles over manifests.
_DETECT_RULES: tuple[tuple[str, str], ...] = (
    ("pnpm-lock.yaml", "pnpm install --frozen-lockfile"),
    ("yarn.lock", "yarn install --frozen-lockfile"),
    ("package-lock.json", "npm ci"),
    ("uv.lock", "uv sync"),
    ("poetry.lock", "poetry install --no-root"),
    ("Pipfile.lock", "pipenv install --deploy"),
    ("Cargo.lock", "cargo fetch"),
    ("go.sum", "go mod download"),
    # Manifest-only fallbacks. Lockfile rules above already covered the
    # paired case; these handle repos that intentionally don't commit a lock.
    ("package.json", "npm install"),
    ("pyproject.toml", "uv sync"),
    ("requirements.txt", "pip install -r requirements.txt"),
    ("Cargo.toml", "cargo fetch"),
    ("go.mod", "go mod download"),
)


def detect_install_command(workspace: Path) -> str | None:
    """Return the inferred install command for *workspace*, or ``None``.

    No fancy heuristics — just inspects the workspace root for known
    lock/manifest files. Returns ``None`` when none match, which the
    manager treats as "no install needed".
    """
    for filename, command in _DETECT_RULES:
        if (workspace / filename).is_file():
            return command
    return None


def resolve_install_command(workspace: Path, *, configured: str | None) -> str | None:
    """Pick the effective install command for *workspace*.

    Resolution order:

    1. ``configured == ""`` — explicit skip; return ``None``.
    2. ``configured is not None`` — use the user-supplied string verbatim.
    3. fall back to :func:`detect_install_command`.
    """
    if configured == "":
        return None
    if configured is not None:
        return configured
    return detect_install_command(workspace)


async def run_install(
    workspace: Path, command: str, *, timeout_seconds: int
) -> InstallResult:
    """Run *command* inside *workspace*; capture stdout+stderr to a log.

    The command is parsed with :func:`shlex.split` and executed without a
    shell, so shell injection from a misconfigured project entry is not a
    risk. Output is streamed to ``workspace/.ccgram-install.log`` for the
    operator to inspect after the fact — this is where ``doctor`` will
    point if an install fails.
    """
    log = install_log_path(workspace)
    log.write_text(f"$ {command}\n", encoding="utf-8")

    argv = shlex.split(command)
    if not argv:
        msg = "install command resolved to empty argv"
        raise ValueError(msg)

    loop = asyncio.get_running_loop()
    start = loop.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"\n[ccgram-pro] command not found: {exc}\n")
        return InstallResult(
            command=command,
            returncode=127,
            duration_seconds=0.0,
            log_path=log,
        )

    async def _drain() -> None:
        assert proc.stdout is not None
        with log.open("a", encoding="utf-8") as fh:
            while True:
                chunk = await proc.stdout.readline()
                if not chunk:
                    break
                fh.write(chunk.decode("utf-8", errors="replace"))
                fh.flush()

    try:
        await asyncio.wait_for(
            asyncio.gather(_drain(), proc.wait()), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"\n[ccgram-pro] install killed after {timeout_seconds}s\n")
        return InstallResult(
            command=command,
            returncode=124,
            duration_seconds=timeout_seconds,
            log_path=log,
        )

    duration = loop.time() - start
    return InstallResult(
        command=command,
        returncode=proc.returncode or 0,
        duration_seconds=duration,
        log_path=log,
    )

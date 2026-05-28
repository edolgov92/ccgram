"""Filesystem-copy provisioning strategy for non-git sources (or opt-in).

Uses ``rsync`` with a built-in exclude list when available — that gives
predictable performance on large repos and skips dependency caches
(``node_modules``, ``.venv``, ``dist`` …) by default. Falls back to
``shutil.copytree`` with the same exclude set when ``rsync`` is missing.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import structlog

logger = structlog.get_logger()


class CopyError(RuntimeError):
    """Raised when filesystem copy provisioning fails."""


# Directories almost certainly safe to skip for a fresh workspace. Lists
# (rather than wildcard globs) so the same shape works for both rsync's
# --exclude and shutil.copytree's ignore callback.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
    "target",
    ".next",
    ".turbo",
    ".cache",
    ".gradle",
    ".idea",
    ".vscode",
    # We MUST copy .git for git-based projects; the manager picks the
    # clone strategy for those anyway, so for the copy strategy keep .git
    # too — the user might want git history available even in a non-clone
    # workspace.
)


_RSYNC_TIMEOUT_SECONDS = 600  # rsync of large trees can take a while


def _rsync_available() -> bool:
    return shutil.which("rsync") is not None


def _rsync_copy(source: Path, dest: Path) -> None:
    args = ["rsync", "-a", "--delete"]
    for pattern in DEFAULT_EXCLUDES:
        args.extend(["--exclude", pattern])
    # rsync semantics: trailing slash on source means "copy CONTENTS into
    # dest"; without it, you get dest/<basename>/... — we want the former.
    args.append(f"{source}/")
    args.append(f"{dest}/")
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=_RSYNC_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        msg = f"rsync failed: {result.stderr.strip() or result.stdout.strip()}"
        raise CopyError(msg)


def _shutil_copy(source: Path, dest: Path) -> None:
    excludes = set(DEFAULT_EXCLUDES)

    def _ignore(_dirname: str, names: list[str]) -> list[str]:
        return [n for n in names if n in excludes]

    try:
        shutil.copytree(source, dest, ignore=_ignore, symlinks=True)
    except (OSError, shutil.Error) as exc:
        msg = f"copytree failed: {exc}"
        raise CopyError(msg) from exc


async def copy_workspace(source: Path, workspace: Path) -> None:
    """Mirror *source* into a fresh *workspace*.

    Stages into a sibling tempdir so the workspace path never exists in a
    half-copied state. Picks ``rsync`` when available, ``shutil`` otherwise.
    """
    if workspace.exists():
        msg = f"Workspace already exists: {workspace}"
        raise CopyError(msg)
    if not source.is_dir():
        msg = f"Source is not a directory: {source}"
        raise CopyError(msg)

    stage_parent = workspace.parent
    stage_parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=workspace.name + ".stage-", dir=stage_parent))

    def _do_work() -> None:
        # mkdtemp returns an existing empty dir; both rsync (trailing
        # slash) and copytree (must not exist) need that handled.
        if _rsync_available():
            _rsync_copy(source, stage)
        else:
            shutil.rmtree(stage)
            _shutil_copy(source, stage)

    try:
        await asyncio.to_thread(_do_work)
        os.replace(stage, workspace)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise

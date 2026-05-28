"""``git`` provisioning strategy — clone a source repo into a workspace.

The clone path uses ``--local --no-hardlinks`` so modifying the workspace's
``.git`` cannot corrupt the source repository's object DB, while still
benefiting from the local-file fast path (no network, near-instant for
typical projects). Uncommitted edits and untracked files in the source can
optionally be carried over so the workspace mirrors what the developer
sees in their main checkout.

This module deliberately matches the subprocess style in
``ccgram.handlers.topics.worktree`` — :class:`GitCloneError` is the
strategy-level exception; the manager translates it into a user-facing
:class:`WorkspaceCreationError`.
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


class GitCloneError(RuntimeError):
    """Raised when a git provisioning step fails."""


_GIT_TIMEOUT_SECONDS = 120


def _git(
    cwd: Path, *args: str, timeout: int = _GIT_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run ``git -C <cwd> <args>`` with stdout + stderr captured.

    Returns the ``CompletedProcess``; callers decide whether non-zero exit
    is fatal (some probes legitimately fail and the caller handles that).
    """
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def is_git_repo(path: Path) -> bool:
    """Return True if *path* is inside a git work tree (not a bare repo)."""
    if not path.is_dir():
        return False
    try:
        result = _git(path, "rev-parse", "--is-inside-work-tree")
    except subprocess.TimeoutExpired, FileNotFoundError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _clone(source: Path, dest: Path) -> None:
    """Clone *source* into *dest* via ``git clone --local --no-hardlinks``.

    ``--no-hardlinks`` copies pack files instead of hardlinking them so a
    later GC inside the workspace cannot reach into the source's object
    database. Slightly slower on cold cache; the safety is worth it.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "git",
            "clone",
            "--local",
            "--no-hardlinks",
            str(source),
            str(dest),
        ],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        msg = f"git clone failed: {result.stderr.strip() or result.stdout.strip()}"
        raise GitCloneError(msg)


def _apply_uncommitted_diff(source: Path, workspace: Path) -> None:
    """Carry the source repo's uncommitted edits into the new workspace.

    Uses ``git diff HEAD --binary`` so binary blobs survive intact. The
    diff is piped to ``git apply --whitespace=nowarn --3way`` inside the
    workspace; an empty diff (clean source) is a no-op. Failure to apply
    is logged but non-fatal — the workspace is still usable, just at HEAD.
    """
    diff_result = _git(source, "diff", "HEAD", "--binary")
    if diff_result.returncode != 0:
        logger.warning(
            "Failed to read source diff for transfer: %s",
            diff_result.stderr.strip(),
        )
        return
    if not diff_result.stdout:
        return  # clean working tree, nothing to apply
    apply_result = subprocess.run(
        ["git", "-C", str(workspace), "apply", "--whitespace=nowarn", "--3way"],
        input=diff_result.stdout,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if apply_result.returncode != 0:
        logger.warning(
            "Could not apply source uncommitted diff to %s (workspace stays at HEAD): %s",
            workspace,
            apply_result.stderr.strip(),
        )


def _copy_untracked_files(source: Path, workspace: Path) -> int:
    """Copy untracked-but-not-ignored files from *source* into *workspace*.

    Uses ``git ls-files --others --exclude-standard`` so .gitignore is
    respected. Returns the number of files copied. Failure to copy any
    single file is logged but does not abort — partial transfer is better
    than no transfer.
    """
    listing = _git(source, "ls-files", "--others", "--exclude-standard", "-z")
    if listing.returncode != 0 or not listing.stdout:
        return 0
    paths = [p for p in listing.stdout.split("\x00") if p]
    copied = 0
    for rel in paths:
        src = source / rel
        if not src.is_file():
            continue
        dst = workspace / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            copied += 1
        except OSError as exc:
            logger.warning("Failed to copy untracked %s: %s", rel, exc)
    return copied


async def clone_workspace(
    source: Path, workspace: Path, *, transfer_uncommitted: bool
) -> None:
    """Provision *workspace* from the git repository at *source*.

    Runs the (synchronous) subprocess work in a worker thread so the
    asyncio event loop stays responsive. If anything before the final move
    fails, the partial workspace is removed so the caller sees a clean
    "not created" state rather than a half-populated directory.
    """
    if workspace.exists():
        msg = f"Workspace already exists: {workspace}"
        raise GitCloneError(msg)

    # Stage into a sibling tempdir, then rename — this keeps a half-cloned
    # workspace from appearing under its final name (where GC could see it).
    stage_parent = workspace.parent
    stage_parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=workspace.name + ".stage-", dir=stage_parent))

    def _do_work() -> None:
        # mkdtemp produced an empty dir; git clone insists on a non-existent
        # target, so remove it and let clone recreate.
        shutil.rmtree(stage)
        _clone(source, stage)
        if transfer_uncommitted:
            _apply_uncommitted_diff(source, stage)
            copied = _copy_untracked_files(source, stage)
            if copied:
                logger.debug(
                    "Copied %d untracked file(s) from %s to %s", copied, source, stage
                )

    try:
        await asyncio.to_thread(_do_work)
        os.replace(stage, workspace)
    except Exception:
        # Best-effort cleanup of the stage directory; the workspace itself
        # was never created (rename never reached), so callers see nothing.
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise

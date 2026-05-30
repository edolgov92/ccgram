"""Commit primitive — stage + commit, with explicit clean-tree handling.

Synchronous (blocking subprocess); async callers wrap in ``asyncio.to_thread``.
"""

from __future__ import annotations

from pathlib import Path

from ._run import run_git
from .preflight import working_tree_status


class NothingToCommit(RuntimeError):  # noqa: N818 -- a state, not an "Error"
    """Raised when there is nothing staged/changed to commit."""


def commit_all(repo: Path | str, message: str, *, add_untracked: bool = True) -> str:
    """Stage changes and commit them; return the new commit SHA.

    ``add_untracked=True`` runs ``git add -A`` (new files included);
    ``False`` runs ``git add -u`` (tracked changes only). Cleanliness is
    checked via porcelain *before* committing so the caller gets a typed
    :class:`NothingToCommit` instead of git's localized "nothing to commit"
    non-zero exit leaking through as a generic error. GPG signing is disabled
    for this commit so a passphrase prompt can't hang the subprocess.
    """
    if not message.strip():
        raise ValueError("commit message is empty")

    status = working_tree_status(repo)
    if add_untracked:
        if status.clean:
            raise NothingToCommit("working tree is clean")
        run_git(repo, "add", "-A")
    else:
        if status.staged == 0 and status.unstaged == 0:
            raise NothingToCommit("no tracked changes to commit")
        run_git(repo, "add", "-u")

    # Re-check: `add -u` with only untracked files stages nothing.
    if not run_git(repo, "diff", "--cached", "--name-only").stdout.strip():
        raise NothingToCommit("nothing staged after add")

    run_git(repo, "-c", "commit.gpgsign=false", "commit", "-m", message)
    return run_git(repo, "rev-parse", "HEAD").stdout.strip()

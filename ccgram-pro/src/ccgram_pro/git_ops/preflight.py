"""Read-only git/gh probes + PR pre-flight validation.

These are *predicates* and validators used by the new-session picker (to gate
the worktree/clone/branch options) and the branch/PR composer (to fail fast
with an actionable message before mutating anything). Every probe runs with
``check=False`` so a normal "false" answer is data, not an exception — only a
genuinely broken environment (no ``git`` on PATH) surfaces as ``GitOpError``,
which the probes translate to ``False``.

All functions are synchronous (blocking subprocess); async callers must wrap
them in ``asyncio.to_thread`` — matching the rest of :mod:`ccgram_pro.git_ops`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ._run import GitOpError, run_git, run_gh


class PRValidationError(RuntimeError):
    """A PR cannot be opened yet; carries a one-line, user-facing reason."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message if not hint else f"{message} — {hint}")
        self.reason = message
        self.hint = hint


@dataclass(frozen=True)
class WorkingTreeStatus:
    staged: int
    unstaged: int
    untracked: int

    @property
    def clean(self) -> bool:
        return self.staged == 0 and self.unstaged == 0 and self.untracked == 0


@dataclass(frozen=True)
class PRReadiness:
    base: str
    head: str
    head_pushed: bool


def is_git_repo(path: Path | str) -> bool:
    """True iff *path* is inside a (non-bare) git work tree."""
    try:
        result = run_git(path, "rev-parse", "--is-inside-work-tree", check=False)
    except GitOpError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def is_detached_head(repo: Path | str) -> bool:
    """True when HEAD is not on a named branch (detached)."""
    try:
        result = run_git(repo, "symbolic-ref", "-q", "HEAD", check=False)
    except GitOpError:
        return False
    return result.returncode != 0


def working_tree_status(repo: Path | str) -> WorkingTreeStatus:
    """Counts of staged / unstaged / untracked entries via ``status --porcelain``."""
    result = run_git(repo, "status", "--porcelain")
    staged = unstaged = untracked = 0
    for line in result.stdout.splitlines():
        if not line:
            continue
        if line.startswith("?? "):
            untracked += 1
            continue
        # Porcelain v1: XY <path>. X = index (staged), Y = work tree (unstaged).
        index_code = line[0]
        worktree_code = line[1] if len(line) > 1 else " "
        if index_code not in (" ", "?"):
            staged += 1
        if worktree_code not in (" ", "?"):
            unstaged += 1
    return WorkingTreeStatus(staged=staged, unstaged=unstaged, untracked=untracked)


def has_uncommitted_changes(repo: Path | str) -> bool:
    """True when the working tree has any staged/unstaged/untracked change."""
    return not working_tree_status(repo).clean


def has_tracked_changes(repo: Path | str) -> bool:
    """True when there are staged or unstaged changes to TRACKED files.

    Excludes untracked files: a branch checkout / fast-forward pull carries them
    across (git never deletes them), so untracked files must not block a branch
    switch. This is the signal the new-session picker uses to gate the
    default-branch option, so tool artifacts like ``.ccgram-uploads/`` or
    ``.claude/`` don't read as "dirty".
    """
    status = working_tree_status(repo)
    return status.staged > 0 or status.unstaged > 0


def gh_is_authenticated() -> bool:
    """True when ``gh auth status`` exits 0 (gh present and logged in)."""
    try:
        result = run_gh("auth", "status", check=False)
    except GitOpError:
        return False
    return result.returncode == 0


def remote_exists(repo: Path | str, remote: str = "origin") -> bool:
    """True when *remote* is configured on the repo."""
    try:
        result = run_git(repo, "remote", check=False)
    except GitOpError:
        return False
    return remote in result.stdout.split()


def branch_exists(repo: Path | str, name: str) -> bool:
    """True when a local branch *name* exists."""
    try:
        result = run_git(
            repo, "show-ref", "--verify", "--quiet", f"refs/heads/{name}", check=False
        )
    except GitOpError:
        return False
    return result.returncode == 0


def remote_branch_exists(repo: Path | str, branch: str, remote: str = "origin") -> bool:
    """True when *branch* exists on *remote* (network call, 60s timeout)."""
    try:
        result = run_git(
            repo, "ls-remote", "--heads", remote, branch, timeout=60, check=False
        )
    except GitOpError:
        return False
    return bool(result.stdout.strip())


def preflight_pull_request(repo: Path | str, *, base: str, head: str) -> PRReadiness:
    """Validate a PR can be opened from *head* into *base*; raise otherwise.

    Ordered so the first actionable problem is reported. ``head_pushed`` is
    returned (not fatal) so the Telegram composer can offer to push, while the
    web composer treats an unpushed head as fatal.
    """
    if not is_git_repo(repo):
        raise PRValidationError("Not a git repository.")
    if not gh_is_authenticated():
        raise PRValidationError(
            "GitHub CLI is not authenticated", hint="run `gh auth login`"
        )
    if base == head:
        raise PRValidationError(f"Base and head are the same branch ({base}).")
    if not remote_exists(repo):
        raise PRValidationError("No 'origin' remote is configured.")
    if not branch_exists(repo, head):
        raise PRValidationError(f"Head branch {head!r} does not exist locally.")
    if not remote_branch_exists(repo, base):
        raise PRValidationError(
            f"Base branch {base!r} was not found on origin",
            hint="push it or pick another base",
        )
    head_pushed = remote_branch_exists(repo, head)
    return PRReadiness(base=base, head=head, head_pushed=head_pushed)

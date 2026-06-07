"""Branch + push primitives. Plain ``git`` subprocess wrappers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ._run import GitOpError, run_git


class PushRejected(RuntimeError):  # noqa: N818 -- a rejected push, not an "Error"
    """A push was rejected (non-fast-forward / remote ahead). Never force-pushed."""


@dataclass(frozen=True)
class BranchInfo:
    name: str
    is_current: bool
    upstream: str | None


def current_branch(repo: Path | str) -> str:
    """Return the currently-checked-out branch name."""
    return run_git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def list_branches(repo: Path | str) -> list[BranchInfo]:
    """Local branches with current + upstream metadata."""
    result = run_git(
        repo,
        "for-each-ref",
        "--format=%(HEAD)\t%(refname:short)\t%(upstream:short)",
        "refs/heads/",
    )
    branches: list[BranchInfo] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        head_marker = parts[0]
        name = parts[1]
        upstream = parts[2] if len(parts) > 2 and parts[2] else None
        branches.append(
            BranchInfo(name=name, is_current=head_marker == "*", upstream=upstream)
        )
    branches.sort(key=lambda b: (not b.is_current, b.name))
    return branches


def checkout(repo: Path | str, ref: str) -> None:
    """Check out an existing ref (branch/commit) in *repo*."""
    run_git(repo, "checkout", ref)


def default_branch(repo: Path | str, *, allow_remote: bool = True) -> str | None:
    """Best-effort resolve the remote's default branch (``main``/``develop``/…).

    Order: the local ``origin/HEAD`` symbolic ref (instant), then — only when
    *allow_remote* is set — a networked ``ls-remote --symref`` (authoritative),
    then a local-name heuristic. Pass ``allow_remote=False`` on latency-sensitive
    paths (e.g. building the picker) to stay fully local. Returns None when
    nothing resolves (e.g. no remote).
    """
    res = run_git(
        repo,
        "symbolic-ref",
        "--quiet",
        "--short",
        "refs/remotes/origin/HEAD",
        check=False,
    )
    if res.returncode == 0 and res.stdout.strip():
        # "origin/main" → "main"
        return res.stdout.strip().split("/", 1)[-1]

    if allow_remote:
        res = run_git(repo, "ls-remote", "--symref", "origin", "HEAD", check=False)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                line = line.strip()
                if line.startswith("ref:") and "HEAD" in line:
                    # "ref: refs/heads/develop\tHEAD" → "develop"
                    return line.split()[1].rsplit("/", 1)[-1]

    for name in ("main", "develop", "master"):
        res = run_git(repo, "rev-parse", "--verify", "--quiet", name, check=False)
        if res.returncode == 0:
            return name
    return None


def has_unpushed_commits(repo: Path | str) -> bool:
    """True when the current branch is ahead of its upstream (unpushed commits).

    False when there's no upstream (can't compare — don't over-report) or when
    the branch is in sync. Never raises.
    """
    upstream = run_git(
        repo,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{u}",
        check=False,
    )
    if upstream.returncode != 0:
        return False
    ahead = run_git(repo, "rev-list", "--count", "@{u}..HEAD", check=False)
    if ahead.returncode != 0:
        return False
    try:
        return int(ahead.stdout.strip()) > 0
    except ValueError:
        return False


def pull_ff_only(repo: Path | str) -> None:
    """``git pull --ff-only``; a no-op when the branch has no upstream.

    A local-only repository (no remote / no tracking branch) has nothing to
    pull — ``git pull`` would fail with "There is no tracking information for
    the current branch". Treat that as a skip rather than an error so a
    remote-less repo (e.g. the local PM workspace) starts cleanly. Still raises
    :class:`GitOpError` when a real upstream exists but the pull is not a
    fast-forward.
    """
    upstream = run_git(
        repo,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{u}",
        check=False,
    )
    if upstream.returncode != 0:
        return  # no upstream tracking branch — nothing to pull
    run_git(repo, "pull", "--ff-only")


def create_branch(
    repo: Path | str, name: str, *, from_ref: str = "HEAD", checkout: bool = True
) -> None:
    """Create *name* off *from_ref*, optionally checking it out."""
    if (
        not name
        or ".." in name
        or name.startswith("/")
        or name.endswith("/")
        or any(c.isspace() for c in name)
    ):
        raise ValueError(f"unsafe branch name: {name!r}")
    if checkout:
        run_git(repo, "checkout", "-b", name, from_ref)
    else:
        run_git(repo, "branch", name, from_ref)


_REJECTED_MARKERS = (
    "non-fast-forward",
    "fetch first",
    "rejected",
    "tip of your current branch is behind",
    "updates were rejected",
)


def push_branch(
    repo: Path | str,
    branch: str | None = None,
    *,
    set_upstream: bool = True,
    remote: str = "origin",
) -> str:
    """Push (and optionally set-upstream) the branch. Returns ``git push`` stdout.

    Classifies a rejected / non-fast-forward push as :class:`PushRejected`
    (the remote has commits the local branch doesn't) instead of a generic
    error. Never force-pushes.
    """
    args: list[str] = ["push"]
    if set_upstream:
        args.extend(["--set-upstream", remote, branch or current_branch(repo)])
    elif branch:
        args.extend([remote, branch])
    result = run_git(repo, *args, timeout=60, check=False)
    if result.returncode == 0:
        return result.stdout
    blob = f"{result.stdout}\n{result.stderr}".lower()
    if any(marker in blob for marker in _REJECTED_MARKERS):
        raise PushRejected(
            "Push rejected — the remote has commits you don't have locally. "
            "Pull/rebase first (no force-push)."
        )
    raise GitOpError(["git", "push", *args], result.returncode, result.stderr)

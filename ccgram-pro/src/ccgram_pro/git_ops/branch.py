"""Branch + push primitives. Plain ``git`` subprocess wrappers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ._run import GitOpError, run_git


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


def create_branch(
    repo: Path | str, name: str, *, from_ref: str = "HEAD", checkout: bool = True
) -> None:
    """Create *name* off *from_ref*, optionally checking it out."""
    if not name or "/" in name and ".." in name:
        raise ValueError(f"unsafe branch name: {name!r}")
    if checkout:
        run_git(repo, "checkout", "-b", name, from_ref)
    else:
        run_git(repo, "branch", name, from_ref)


def push_branch(
    repo: Path | str,
    branch: str | None = None,
    *,
    set_upstream: bool = True,
    remote: str = "origin",
) -> str:
    """Push (and optionally set-upstream) the branch. Returns ``git push`` stdout."""
    args: list[str] = ["push"]
    if set_upstream:
        args.extend(["--set-upstream", remote, branch or current_branch(repo)])
    else:
        if branch:
            args.extend([remote, branch])
    return run_git(repo, *args, timeout=60).stdout

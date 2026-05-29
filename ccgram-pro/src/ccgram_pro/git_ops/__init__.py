"""Thin subprocess wrappers around ``git`` and ``gh`` for the layer.

Matches the style of ``ccgram.handlers.topics.worktree._git`` —
plain ``subprocess.run`` with ``-C <path>`` plumbing, captured output,
short timeouts. No GitPython, no libgit2. Wraps each operation in a
typed helper so callers don't have to remember the flag soup.
"""

from .branch import (
    BranchInfo,
    create_branch,
    current_branch,
    list_branches,
    push_branch,
)
from ._run import GitOpError
from .diff import (
    DiffFile,
    DiffHunk,
    capture_diff_vs_ref,
    parse_unified_diff,
)
from .pr import PullRequestError, create_pull_request, list_pull_requests
from .snapshot import (
    DiffSnapshot,
    SnapshotNotFound,
    list_snapshots,
    load_snapshot,
    save_snapshot,
)

__all__ = [
    "BranchInfo",
    "DiffFile",
    "DiffHunk",
    "DiffSnapshot",
    "GitOpError",
    "PullRequestError",
    "SnapshotNotFound",
    "capture_diff_vs_ref",
    "create_branch",
    "create_pull_request",
    "current_branch",
    "list_branches",
    "list_pull_requests",
    "list_snapshots",
    "load_snapshot",
    "parse_unified_diff",
    "push_branch",
    "save_snapshot",
]

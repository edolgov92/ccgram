"""Thin subprocess wrappers around ``git`` and ``gh`` for the layer.

Matches the style of ``ccgram.handlers.topics.worktree._git`` —
plain ``subprocess.run`` with ``-C <path>`` plumbing, captured output,
short timeouts. No GitPython, no libgit2. Wraps each operation in a
typed helper so callers don't have to remember the flag soup.

Everything here is synchronous; async callers wrap calls in
``asyncio.to_thread`` so the event loop is never blocked.
"""

from .branch import (
    BranchInfo,
    PushRejected,
    checkout,
    create_branch,
    current_branch,
    list_branches,
    push_branch,
)
from .commit import NothingToCommit, commit_all
from ._run import GitOpError
from .diff import (
    DiffFile,
    DiffHunk,
    capture_diff_vs_ref,
    parse_unified_diff,
)
from .pr import (
    PullRequestError,
    PullRequestSummary,
    create_pull_request,
    list_pull_requests,
)
from .preflight import (
    PRReadiness,
    PRValidationError,
    WorkingTreeStatus,
    branch_exists,
    gh_is_authenticated,
    has_uncommitted_changes,
    is_detached_head,
    is_git_repo,
    preflight_pull_request,
    remote_branch_exists,
    remote_exists,
    working_tree_status,
)
from .snapshot import (
    SnapshotEntry,
    SnapshotIndex,
    capture_snapshot,
    delete_window_snapshots,
    diff_between,
    file_content_at,
    latest_n,
    load_index,
    prune_snapshots,
)

__all__ = [
    "BranchInfo",
    "DiffFile",
    "DiffHunk",
    "GitOpError",
    "NothingToCommit",
    "PRReadiness",
    "PRValidationError",
    "PullRequestError",
    "PullRequestSummary",
    "PushRejected",
    "SnapshotEntry",
    "SnapshotIndex",
    "WorkingTreeStatus",
    "branch_exists",
    "capture_diff_vs_ref",
    "capture_snapshot",
    "checkout",
    "commit_all",
    "create_branch",
    "create_pull_request",
    "current_branch",
    "delete_window_snapshots",
    "diff_between",
    "file_content_at",
    "gh_is_authenticated",
    "has_uncommitted_changes",
    "is_detached_head",
    "is_git_repo",
    "latest_n",
    "list_branches",
    "list_pull_requests",
    "load_index",
    "parse_unified_diff",
    "preflight_pull_request",
    "prune_snapshots",
    "push_branch",
    "remote_branch_exists",
    "remote_exists",
    "working_tree_status",
]

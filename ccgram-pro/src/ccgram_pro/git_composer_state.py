"""Per-thread input-capture state for the Git/PR composer.

The composer occasionally needs a free-text reply (branch name, commit
message, PR title/body). Riding ccgram's per-USER ``context.user_data`` would
let two topics clobber each other and would collide with ccgram's own
``AWAITING_WORKTREE_BRANCH_NAME`` squat. Instead we key by
``(user_id, thread_id)`` in a module-level dict — the same robust pattern as
``input_pipeline.intercept._status_messages``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ComposerInput:
    """What the next text message in this thread should be captured as."""

    awaiting: str  # "branch_name" | "commit_message" | "pr_title" | "pr_body"
    window_id: str
    repo: str
    # PR composition carried across steps.
    pr_title: str = ""
    pr_body: str = ""
    base: str = ""
    base_choices: list[str] = field(default_factory=list)
    base_idx: int = 0
    draft: bool = False
    suggested_branch: str = ""


_pending: dict[tuple[int, int], ComposerInput] = {}


def arm(user_id: int, thread_id: int, state: ComposerInput) -> None:
    _pending[(user_id, thread_id)] = state


def peek(user_id: int, thread_id: int) -> ComposerInput | None:
    return _pending.get((user_id, thread_id))


def disarm(user_id: int, thread_id: int) -> ComposerInput | None:
    return _pending.pop((user_id, thread_id), None)


def _reset_for_testing() -> None:
    _pending.clear()

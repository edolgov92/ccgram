"""Immutable per-iteration diff snapshots via a private git ref namespace.

The old model stored ``git diff HEAD`` text per Stop — which collapsed the
moment the user committed (a clean working tree diffs to nothing) and could
never survive a commit / push / branch switch. This model instead freezes the
COMPLETE working tree (tracked + untracked, respecting ``.gitignore``) as a real
git commit at each iteration boundary, WITHOUT touching the user's index or
working tree:

1. ``GIT_INDEX_FILE=<tmp>`` (outside the work tree) → ``read-tree HEAD`` →
   ``add -A`` → ``write-tree`` ⇒ a tree SHA capturing the current state.
2. ``commit-tree <tree> [-p <prev>] -m …`` ⇒ a commit chained onto the previous
   snapshot (with an injected identity so it works on repos lacking user.email).
3. ``update-ref refs/ccgram-pro/snapshots/<window> <commit>`` ⇒ one local head
   ref per window. Because each commit's parent is the prior snapshot, this
   single ref keeps the WHOLE chain reachable — gc-safe, never pushed, and
   independent of where the user's HEAD/branches move.

Diffs are then computed on demand between two frozen snapshot commits:

- **Last iteration** = ``diff(snap[N-1], snap[N])``
- **Since session start** = ``diff(snap[0], snap[N])``

so they stay correct after commits, pushes, and branch switches. ``index.json``
under ``<layer_dir>/snapshots/<window>/`` records the ordered (n → commit) map
plus display metadata.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from ccgram.utils import atomic_write_json

from ..config import snapshot_dir
from ._run import GitOpError, run_git

logger = structlog.get_logger()

_REF_ROOT = "refs/ccgram-pro/snapshots"
_SNAPSHOT_TIMEOUT = 120  # `git add -A` over a large tree can be slow
# Injected so commit-tree succeeds even when the repo has no user.name/email.
_IDENTITY = (
    "-c",
    "user.name=ccgram-pro",
    "-c",
    "user.email=ccgram-pro@localhost",
)


@dataclass(frozen=True)
class SnapshotEntry:
    """One frozen iteration boundary."""

    n: int  # 0 = session anchor
    commit_sha: str
    tree_sha: str
    real_head_sha: str  # the user's actual HEAD at capture (display only)
    branch: str  # abbrev-ref HEAD ("HEAD" when detached)
    captured_at: float
    has_changes: bool  # tree differs from the previous snapshot's tree


@dataclass(frozen=True)
class SnapshotIndex:
    """All snapshots for a window + the repo they were captured from."""

    window_id: str
    project_root: str
    entries: list[SnapshotEntry]


def _sani(window_id: str) -> str:
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.mailbox import sanitize_dir_name

    return sanitize_dir_name(window_id)


def _window_dir(window_id: str) -> Path:
    return snapshot_dir() / _sani(window_id)


def _index_path(window_id: str) -> Path:
    return _window_dir(window_id) / "index.json"


def _ref_name(window_id: str) -> str:
    return f"{_REF_ROOT}/{_sani(window_id)}"


def load_index(window_id: str) -> SnapshotIndex | None:
    """Read the snapshot index for *window_id*, or ``None`` if absent/corrupt."""
    path = _index_path(window_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("corrupt snapshot index %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    entries: list[SnapshotEntry] = []
    for raw in data.get("entries", []):
        if not isinstance(raw, dict):
            continue
        try:
            entries.append(
                SnapshotEntry(
                    n=int(raw["n"]),
                    commit_sha=str(raw["commit_sha"]),
                    tree_sha=str(raw["tree_sha"]),
                    real_head_sha=str(raw.get("real_head_sha", "")),
                    branch=str(raw.get("branch", "")),
                    captured_at=float(raw.get("captured_at", 0.0)),
                    has_changes=bool(raw.get("has_changes", False)),
                )
            )
        except KeyError, TypeError, ValueError:
            continue
    return SnapshotIndex(
        window_id=str(data.get("window_id", window_id)),
        project_root=str(data.get("project_root", "")),
        entries=entries,
    )


def _save_index(index: SnapshotIndex) -> None:
    _window_dir(index.window_id).mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        _index_path(index.window_id),
        {
            "v": 1,
            "window_id": index.window_id,
            "project_root": index.project_root,
            "entries": [
                {
                    "n": e.n,
                    "commit_sha": e.commit_sha,
                    "tree_sha": e.tree_sha,
                    "real_head_sha": e.real_head_sha,
                    "branch": e.branch,
                    "captured_at": e.captured_at,
                    "has_changes": e.has_changes,
                }
                for e in index.entries
            ],
        },
    )


def _write_working_tree(repo: Path | str, env: dict[str, str]) -> str:
    """Stage the full working tree into the private index and write a tree SHA."""
    head = run_git(repo, "rev-parse", "HEAD", check=False)
    if head.returncode == 0 and head.stdout.strip():
        run_git(repo, "read-tree", "HEAD", env=env, timeout=_SNAPSHOT_TIMEOUT)
    run_git(repo, "add", "-A", env=env, timeout=_SNAPSHOT_TIMEOUT)
    return run_git(
        repo, "write-tree", env=env, timeout=_SNAPSHOT_TIMEOUT
    ).stdout.strip()


def capture_snapshot(*, window_id: str, project_root: Path | str) -> SnapshotEntry:
    """Freeze the current working tree as the next snapshot. Raises GitOpError.

    Uses a temp index OUTSIDE the repo so the user's index/working tree are never
    touched. The new commit chains onto the previous snapshot via ``-p`` so a
    single head ref keeps every snapshot reachable.
    """
    repo = str(project_root)
    real_head = run_git(repo, "rev-parse", "HEAD", check=False)
    real_head_sha = real_head.stdout.strip() if real_head.returncode == 0 else ""
    branch_res = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    branch = branch_res.stdout.strip() if branch_res.returncode == 0 else ""

    # The index must NOT pre-exist (git rejects a 0-byte file as a corrupt
    # index), so use a fresh temp dir and a not-yet-created path inside it.
    tmp_dir = tempfile.mkdtemp(prefix="ccgrampro-idx-")
    try:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(tmp_dir, "index")}
        tree_sha = _write_working_tree(repo, env)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    index = load_index(window_id)
    prev = index.entries[-1] if index and index.entries else None
    n = prev.n + 1 if prev else 0
    parents = ["-p", prev.commit_sha] if prev else []
    commit_sha = run_git(
        repo,
        *_IDENTITY,
        "commit-tree",
        tree_sha,
        *parents,
        "-m",
        f"ccgram-pro snapshot {window_id} #{n}",
    ).stdout.strip()
    run_git(repo, "update-ref", _ref_name(window_id), commit_sha)

    entry = SnapshotEntry(
        n=n,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        real_head_sha=real_head_sha,
        branch=branch,
        captured_at=time.time(),
        has_changes=bool(prev and tree_sha != prev.tree_sha),
    )
    new_entries = [*(index.entries if index else []), entry]
    _save_index(
        SnapshotIndex(
            window_id=window_id,
            project_root=(index.project_root if index else repo) or repo,
            entries=new_entries,
        )
    )
    logger.debug(
        "snapshot captured: window=%s n=%d tree=%s changed=%s",
        window_id,
        n,
        tree_sha[:8],
        entry.has_changes,
    )
    return entry


def latest_n(window_id: str) -> int | None:
    """Return the highest snapshot index for *window_id*, or ``None``."""
    index = load_index(window_id)
    if not index or not index.entries:
        return None
    return index.entries[-1].n


def session_base_n(index: SnapshotIndex) -> int:
    """Branch-aware base index for the "since session start" diff.

    The literal ``n=0`` anchor can sit on a *different* branch than the latest
    snapshot when the user switched branches mid-session. Diffing two full
    working-tree snapshots across that boundary floods the view with the entire
    inter-branch delta — files the session never touched. So anchor to the
    earliest snapshot that shares the latest snapshot's branch; fall back to the
    earliest snapshot when the session stayed on one branch, when HEAD is
    detached (``"HEAD"``), or when branch info is missing.
    """
    if not index.entries:
        return 0
    latest_branch = index.entries[-1].branch
    if not latest_branch or latest_branch == "HEAD":
        return index.entries[0].n
    for entry in index.entries:
        if entry.branch == latest_branch:
            return entry.n
    return index.entries[0].n


def _commit_for(index: SnapshotIndex, n: int) -> str | None:
    for entry in index.entries:
        if entry.n == n:
            return entry.commit_sha
    return None


def diff_between(
    window_id: str, *, base_n: int, target_n: int, unified: int = 3
) -> str:
    """Return the unified diff between two frozen snapshots (empty if equal)."""
    index = load_index(window_id)
    if index is None:
        return ""
    if base_n == target_n:
        return ""
    base = _commit_for(index, base_n)
    target = _commit_for(index, target_n)
    if base is None or target is None:
        return ""
    result = run_git(
        index.project_root,
        "diff",
        f"--unified={unified}",
        "--no-color",
        base,
        target,
        check=False,
    )
    return result.stdout


def file_content_at(window_id: str, *, n: int, path: str) -> str | None:
    """Return the full content of *path* at snapshot *n*, or ``None`` if absent."""
    index = load_index(window_id)
    if index is None:
        return None
    commit = _commit_for(index, n)
    if commit is None:
        return None
    result = run_git(index.project_root, "show", f"{commit}:{path}", check=False)
    if result.returncode != 0:
        return None
    return result.stdout


def prune_snapshots(*, prune_after_days: int, now: float | None = None) -> int:
    """Delete snapshot dirs (+ refs) for windows idle past *prune_after_days*.

    Also reaps orphan dirs whose index is unreadable. Returns the count removed.
    """
    root = snapshot_dir()
    if not root.is_dir():
        return 0
    effective_now = time.time() if now is None else now
    cutoff = effective_now - prune_after_days * 86400
    removed = 0
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        index_path = entry / "index.json"
        if not index_path.is_file():
            # Orphan or legacy (old session/iteration files) — drop it.
            with contextlib.suppress(OSError):
                shutil.rmtree(entry)
                removed += 1
            continue
        index = _load_index_from(index_path)
        if index is None or not index.entries:
            with contextlib.suppress(OSError):
                shutil.rmtree(entry)
                removed += 1
            continue
        if index.entries[-1].captured_at > cutoff:
            continue
        _delete_window_refs(index)
        with contextlib.suppress(OSError):
            shutil.rmtree(entry)
            removed += 1
    if removed:
        logger.info("pruned %d stale diff-snapshot window(s)", removed)
    return removed


def _load_index_from(index_path: Path) -> SnapshotIndex | None:
    # Resolve the window id from the dir name is lossy (sanitized); the index
    # stores the real window_id, so load by path then trust its window_id.
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    window_id = str(data.get("window_id", "")) if isinstance(data, dict) else ""
    if not window_id:
        return None
    return load_index(window_id)


def delete_window_snapshots(window_id: str) -> None:
    """Remove a window's snapshot dir + git ref (used by session teardown)."""
    index = load_index(window_id)
    if index is not None:
        _delete_window_refs(index)
    with contextlib.suppress(OSError):
        shutil.rmtree(_window_dir(window_id))


def _delete_window_refs(index: SnapshotIndex) -> None:
    """Best-effort delete the window's snapshot ref so objects become gc-able."""
    if not index.project_root:
        return
    with contextlib.suppress(GitOpError):
        run_git(
            index.project_root,
            "update-ref",
            "-d",
            _ref_name(index.window_id),
            check=False,
        )


__all__ = [
    "GitOpError",
    "SnapshotEntry",
    "SnapshotIndex",
    "capture_snapshot",
    "delete_window_snapshots",
    "diff_between",
    "file_content_at",
    "latest_n",
    "load_index",
    "prune_snapshots",
]

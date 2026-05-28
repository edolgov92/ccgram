"""Per-Stop diff snapshots stored under ``<layer_dir>/snapshots/<window_id>/``.

Two snapshots per window matter for the diff toggle:

- **session anchor** — captured on the first user message of the session
  so "what's changed this whole session" is well-defined even when the
  user makes commits mid-session.
- **rolling iteration anchor** — updated on every Stop. ``current vs
  iteration`` is "what changed since Claude last finished".

Both are stored as ``(sha, patch_text, captured_at)``. The web viewer
chooses between them with a toggle.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from ..config import snapshot_dir
from ._run import GitOpError, run_git
from .diff import capture_diff_vs_ref

logger = structlog.get_logger()


@dataclass(frozen=True)
class DiffSnapshot:
    """A point-in-time anchor for a window's diff comparisons."""

    window_id: str
    label: str  # "session" | "iteration"
    head_sha: str
    branch: str
    captured_at: float
    diff_text: str
    project_root: str


class SnapshotNotFound(LookupError):
    """No anchor of that label exists for the given window."""


def _window_dir(window_id: str) -> Path:
    # Reuse mailbox sanitisation rules — same as elsewhere in the layer.
    from ccgram.mailbox import sanitize_dir_name

    return snapshot_dir() / sanitize_dir_name(window_id)


def _meta_path(window_id: str, label: str) -> Path:
    return _window_dir(window_id) / f"{label}.json"


def save_snapshot(
    *,
    window_id: str,
    label: str,
    project_root: Path | str,
) -> DiffSnapshot:
    """Capture HEAD + diff vs HEAD at *project_root* and persist as *label*.

    Errors from git (e.g. project_root isn't a repo) raise
    :class:`GitOpError` — callers should catch and downgrade to a log.
    """
    if label not in ("session", "iteration"):
        raise ValueError(f"unknown snapshot label: {label!r}")
    head_result = run_git(project_root, "rev-parse", "HEAD")
    branch_result = run_git(project_root, "rev-parse", "--abbrev-ref", "HEAD")
    diff_text = capture_diff_vs_ref(project_root, "HEAD")
    snap = DiffSnapshot(
        window_id=window_id,
        label=label,
        head_sha=head_result.stdout.strip(),
        branch=branch_result.stdout.strip(),
        captured_at=time.time(),
        diff_text=diff_text,
        project_root=str(project_root),
    )
    target_dir = _window_dir(window_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "window_id": snap.window_id,
        "label": snap.label,
        "head_sha": snap.head_sha,
        "branch": snap.branch,
        "captured_at": snap.captured_at,
        "project_root": snap.project_root,
        "diff_bytes": len(snap.diff_text),
    }
    _meta_path(window_id, label).write_text(
        json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
    )
    (target_dir / f"{label}.patch").write_text(snap.diff_text, encoding="utf-8")
    logger.debug(
        "snapshot saved: window=%s label=%s sha=%s bytes=%d",
        window_id,
        label,
        snap.head_sha[:8],
        len(snap.diff_text),
    )
    return snap


def load_snapshot(window_id: str, label: str) -> DiffSnapshot:
    """Read a previously-saved snapshot."""
    meta_path = _meta_path(window_id, label)
    patch_path = meta_path.with_suffix(".patch")
    if not meta_path.is_file() or not patch_path.is_file():
        raise SnapshotNotFound(f"no {label} snapshot for {window_id}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        diff_text = patch_path.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotNotFound(f"corrupt snapshot {window_id}/{label}: {exc}") from exc
    return DiffSnapshot(
        window_id=str(meta.get("window_id", window_id)),
        label=str(meta.get("label", label)),
        head_sha=str(meta.get("head_sha", "")),
        branch=str(meta.get("branch", "")),
        captured_at=float(meta.get("captured_at", 0.0)),
        diff_text=diff_text,
        project_root=str(meta.get("project_root", "")),
    )


def list_snapshots(window_id: str) -> list[str]:
    """Return the snapshot labels present on disk for *window_id*."""
    root = _window_dir(window_id)
    if not root.is_dir():
        return []
    return sorted(
        p.stem for p in root.iterdir() if p.suffix == ".json" and p.is_file()
    )


__all__ = [
    "DiffSnapshot",
    "GitOpError",
    "SnapshotNotFound",
    "list_snapshots",
    "load_snapshot",
    "save_snapshot",
]

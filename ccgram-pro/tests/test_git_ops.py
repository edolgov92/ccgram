"""Tests for ``ccgram_pro.git_ops`` — diff parser + snapshot store + branch ops."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from ccgram_pro.git_ops import (
    BranchInfo,
    DiffSnapshot,
    SnapshotNotFound,
    capture_diff_vs_ref,
    create_branch,
    current_branch,
    list_branches,
    list_snapshots,
    load_snapshot,
    parse_unified_diff,
    save_snapshot,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _init_repo(root: Path) -> Path:
    repo = root / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


# ── diff capture + parse ────────────────────────────────────────────────


def test_capture_diff_empty_when_no_changes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    assert capture_diff_vs_ref(repo, "HEAD") == ""


def test_capture_diff_picks_up_uncommitted_edits(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("hello\nworld\n")
    raw = capture_diff_vs_ref(repo, "HEAD")
    assert "+world" in raw
    assert "diff --git" in raw


def test_parse_unified_diff_simple_add(tmp_path: Path) -> None:
    raw = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,3 +1,4 @@\n"
        " line1\n"
        " line2\n"
        "+inserted\n"
        " line3\n"
    )
    files = parse_unified_diff(raw)
    assert len(files) == 1
    f = files[0]
    assert f.path == "foo.py"
    assert f.old_path == "foo.py"
    assert not f.binary
    assert len(f.hunks) == 1
    hunk = f.hunks[0]
    assert hunk.old_start == 1
    assert hunk.new_start == 1
    markers = [m for m, _ in hunk.lines]
    assert markers == [" ", " ", "+", " "]


def test_parse_unified_diff_binary(tmp_path: Path) -> None:
    raw = (
        "diff --git a/img.png b/img.png\n"
        "Binary files a/img.png and b/img.png differ\n"
    )
    files = parse_unified_diff(raw)
    assert len(files) == 1
    assert files[0].binary is True
    assert files[0].hunks == []


def test_parse_unified_diff_multiple_files(tmp_path: Path) -> None:
    raw = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/b.py b/b.py\n"
        "--- a/b.py\n"
        "+++ b/b.py\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    files = parse_unified_diff(raw)
    assert [f.path for f in files] == ["a.py", "b.py"]


# ── snapshot store ──────────────────────────────────────────────────────


def test_save_and_load_snapshot(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("hello\nworld\n")
    snap = save_snapshot(window_id="@x", label="iteration", project_root=repo)
    assert isinstance(snap, DiffSnapshot)
    assert snap.label == "iteration"
    assert "world" in snap.diff_text
    loaded = load_snapshot("@x", "iteration")
    assert loaded.diff_text == snap.diff_text
    assert loaded.head_sha == snap.head_sha


def test_load_snapshot_raises_on_missing(tmp_path: Path) -> None:
    with pytest.raises(SnapshotNotFound):
        load_snapshot("@nope", "iteration")


def test_list_snapshots_returns_labels(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("hello\nworld\n")
    save_snapshot(window_id="@y", label="session", project_root=repo)
    save_snapshot(window_id="@y", label="iteration", project_root=repo)
    labels = list_snapshots("@y")
    assert set(labels) == {"session", "iteration"}


def test_save_snapshot_rejects_unknown_label(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    with pytest.raises(ValueError, match="unknown snapshot label"):
        save_snapshot(window_id="@x", label="weekly", project_root=repo)


# ── branch ops ──────────────────────────────────────────────────────────


def test_current_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    assert current_branch(repo) == "main"


def test_list_branches_marks_current(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    branches = list_branches(repo)
    assert len(branches) == 1
    assert isinstance(branches[0], BranchInfo)
    assert branches[0].name == "main"
    assert branches[0].is_current is True


def test_create_branch_switches_to_new(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    create_branch(repo, "feature/x")
    assert current_branch(repo) == "feature/x"
    names = {b.name for b in list_branches(repo)}
    assert names == {"main", "feature/x"}


def test_create_branch_no_checkout(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    create_branch(repo, "feature/staging", checkout=False)
    assert current_branch(repo) == "main"
    assert "feature/staging" in {b.name for b in list_branches(repo)}

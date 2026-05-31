"""Tests for ``ccgram_pro.git_ops`` — diff parser + snapshot store + branch ops."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ccgram_pro.git_ops import (
    BranchInfo,
    capture_diff_vs_ref,
    capture_snapshot,
    create_branch,
    current_branch,
    delete_window_snapshots,
    diff_between,
    file_content_at,
    latest_n,
    list_branches,
    load_index,
    parse_unified_diff,
    prune_snapshots,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _status(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _object_type(repo: Path, sha: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-t", sha],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


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
        "diff --git a/img.png b/img.png\nBinary files a/img.png and b/img.png differ\n"
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


def test_capture_creates_anchor_n0(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    entry = capture_snapshot(window_id="@x", project_root=repo)
    assert entry.n == 0
    assert entry.has_changes is False
    assert _object_type(repo, entry.commit_sha) == "commit"
    assert latest_n("@x") == 0


def test_capture_does_not_touch_working_tree(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("hello\nchanged\n")  # modify tracked
    (repo / "new.txt").write_text("untracked\n")
    before = _status(repo)
    capture_snapshot(window_id="@x", project_root=repo)
    assert _status(repo) == before  # no temp-index leakage, no staging


def test_capture_includes_untracked_respects_gitignore(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    capture_snapshot(window_id="@x", project_root=repo)  # n0 pristine
    (repo / ".gitignore").write_text("blocked.txt\n")
    (repo / "tracked_new.txt").write_text("kept\n")
    (repo / "blocked.txt").write_text("SECRETDATA\n")
    capture_snapshot(window_id="@x", project_root=repo)  # n1
    diff = diff_between("@x", base_n=0, target_n=1)
    assert "tracked_new.txt" in diff
    # The gitignored file's own header + content must be absent from the tree.
    assert "b/blocked.txt" not in diff
    assert "SECRETDATA" not in diff
    assert file_content_at("@x", n=1, path="tracked_new.txt") == "kept\n"
    assert file_content_at("@x", n=1, path="blocked.txt") is None


def test_last_iteration_vs_session(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    capture_snapshot(window_id="@x", project_root=repo)  # n0
    (repo / "a.txt").write_text("first change\n")
    capture_snapshot(window_id="@x", project_root=repo)  # n1
    (repo / "b.txt").write_text("second change\n")
    capture_snapshot(window_id="@x", project_root=repo)  # n2
    last = diff_between("@x", base_n=1, target_n=2)
    assert "b.txt" in last and "a.txt" not in last
    session = diff_between("@x", base_n=0, target_n=2)
    assert "a.txt" in session and "b.txt" in session


def test_no_change_iteration_is_empty(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    capture_snapshot(window_id="@x", project_root=repo)  # n0
    (repo / "a.txt").write_text("change\n")
    capture_snapshot(window_id="@x", project_root=repo)  # n1
    e2 = capture_snapshot(window_id="@x", project_root=repo)  # n2, no change
    assert e2.has_changes is False
    assert diff_between("@x", base_n=1, target_n=2) == ""
    assert "a.txt" in diff_between("@x", base_n=0, target_n=2)


def test_survives_commit_and_branch_switch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    capture_snapshot(window_id="@x", project_root=repo)  # n0
    (repo / "a.txt").write_text("work\n")
    capture_snapshot(window_id="@x", project_root=repo)  # n1
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "commit the work")
    _git(repo, "checkout", "-q", "-b", "other")
    # The frozen snapshots still resolve and show the same content.
    assert "a.txt" in diff_between("@x", base_n=0, target_n=1)


def test_empty_repo_no_head(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f.txt").write_text("content\n")
    entry = capture_snapshot(window_id="@e", project_root=repo)
    assert entry.n == 0
    assert _object_type(repo, entry.commit_sha) == "commit"


def test_file_content_at_missing_returns_none(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    capture_snapshot(window_id="@x", project_root=repo)
    assert file_content_at("@x", n=0, path="README.md") == "hello\n"
    assert file_content_at("@x", n=0, path="nope/missing.py") is None


def test_load_index_absent_returns_none() -> None:
    assert load_index("@never") is None
    assert latest_n("@never") is None


def test_prune_removes_stale_window(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    capture_snapshot(window_id="@old", project_root=repo)
    assert load_index("@old") is not None
    removed = prune_snapshots(prune_after_days=1, now=2_000_000_000.0)
    assert removed >= 1
    assert load_index("@old") is None


def test_delete_window_snapshots(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    capture_snapshot(window_id="@x", project_root=repo)
    delete_window_snapshots("@x")
    assert load_index("@x") is None


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

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from ccgram_pro.git_ops import NothingToCommit, commit_all, working_tree_status


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


def test_commit_clean_tree_raises(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    with pytest.raises(NothingToCommit):
        commit_all(repo, "noop")


def test_commit_empty_message_raises(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("changed\n")
    with pytest.raises(ValueError):
        commit_all(repo, "   ")


def test_commit_staged_and_unstaged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("changed\n")
    (repo / "new.txt").write_text("new\n")
    sha = commit_all(repo, "feat: change")
    assert len(sha) == 40
    assert working_tree_status(repo).clean


def test_commit_untracked_only_without_add_untracked_raises(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "new.txt").write_text("new\n")
    with pytest.raises(NothingToCommit):
        commit_all(repo, "feat: x", add_untracked=False)

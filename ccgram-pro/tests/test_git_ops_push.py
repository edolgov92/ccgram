from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from ccgram_pro.git_ops import PushRejected, push_branch


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _init_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _bare_remote(root: Path) -> Path:
    bare = root / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(bare)],
        check=True,
        capture_output=True,
    )
    return bare


def test_push_sets_upstream(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "r")
    bare = _bare_remote(tmp_path)
    _git(repo, "remote", "add", "origin", str(bare))
    push_branch(repo, set_upstream=True)
    # remote now has main
    out = subprocess.run(
        ["git", "-C", str(bare), "branch", "--list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "main" in out


def test_push_rejected_on_divergence(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "r")
    bare = _bare_remote(tmp_path)
    _git(repo, "remote", "add", "origin", str(bare))
    push_branch(repo, set_upstream=True)

    # A second clone advances the remote.
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(clone)], check=True, capture_output=True
    )
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "T")
    (clone / "README.md").write_text("remote change\n")
    _git(clone, "commit", "-aqm", "remote")
    _git(clone, "push", "-q", "origin", "main")

    # Local diverges and push must be rejected (never force-pushed).
    (repo / "README.md").write_text("local change\n")
    _git(repo, "commit", "-aqm", "local")
    with pytest.raises(PushRejected):
        push_branch(repo, set_upstream=False, branch="main")

from __future__ import annotations

import subprocess
from pathlib import Path

from ccgram_pro.git_ops import (
    default_branch,
    has_unpushed_commits,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _clone_with_origin(tmp_path: Path, *, default: str = "main") -> Path:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "-q", "--bare", "-b", default)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", default)
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "T")
    (seed / "f").write_text("x\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "init")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", default)

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(remote), str(clone))
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "T")
    return clone


def test_default_branch_main_from_origin_head(tmp_path: Path) -> None:
    clone = _clone_with_origin(tmp_path, default="main")
    assert default_branch(clone, allow_remote=False) == "main"


def test_default_branch_develop_from_origin_head(tmp_path: Path) -> None:
    clone = _clone_with_origin(tmp_path, default="develop")
    assert default_branch(clone, allow_remote=False) == "develop"


def test_default_branch_none_without_remote(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "f").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    # No origin and none of main/master/develop exist → None.
    assert default_branch(repo, allow_remote=False) is None


def test_has_unpushed_commits_tracks_ahead_of_upstream(tmp_path: Path) -> None:
    clone = _clone_with_origin(tmp_path)
    assert has_unpushed_commits(clone) is False
    (clone / "g").write_text("y\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-qm", "local-only")
    assert has_unpushed_commits(clone) is True


def test_has_unpushed_commits_false_without_upstream(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "f").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    # No upstream configured → can't compare → False (don't over-report).
    assert has_unpushed_commits(repo) is False

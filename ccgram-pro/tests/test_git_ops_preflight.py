from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from ccgram_pro.git_ops import (
    PRValidationError,
    branch_exists,
    has_uncommitted_changes,
    is_detached_head,
    is_git_repo,
    preflight_pull_request,
    remote_exists,
    working_tree_status,
)
from ccgram_pro.git_ops import preflight as pf


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


def test_is_git_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    assert is_git_repo(repo) is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert is_git_repo(plain) is False


def test_detached_head(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    assert is_detached_head(repo) is False
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git(repo, "checkout", "-q", sha)
    assert is_detached_head(repo) is True


def test_working_tree_status_counts(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    assert working_tree_status(repo).clean
    (repo / "README.md").write_text("changed\n")
    (repo / "untracked.txt").write_text("u\n")
    st = working_tree_status(repo)
    assert st.unstaged == 1
    assert st.untracked == 1
    assert has_uncommitted_changes(repo)


def test_branch_and_remote_probes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    assert branch_exists(repo, "main") is True
    assert branch_exists(repo, "nope") is False
    assert remote_exists(repo) is False
    _git(repo, "remote", "add", "origin", "https://example.com/x.git")
    assert remote_exists(repo) is True


def test_preflight_rejects_same_base_head(tmp_path: Path, monkeypatch) -> None:
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(pf, "gh_is_authenticated", lambda: True)
    with pytest.raises(PRValidationError):
        preflight_pull_request(repo, base="main", head="main")


def test_preflight_rejects_unauthenticated_gh(tmp_path: Path, monkeypatch) -> None:
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(pf, "gh_is_authenticated", lambda: False)
    with pytest.raises(PRValidationError) as exc:
        preflight_pull_request(repo, base="main", head="feat")
    assert "auth" in str(exc.value).lower()

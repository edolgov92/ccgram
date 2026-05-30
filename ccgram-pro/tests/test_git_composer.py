from __future__ import annotations

import pytest
from ccgram_pro import git_composer as gc
from ccgram_pro import git_composer_state as gstate
from ccgram_pro import state


@pytest.fixture(autouse=True)
def _reset():
    gstate._reset_for_testing()
    yield
    gstate._reset_for_testing()


def test_state_arm_peek_disarm() -> None:
    ci = gstate.ComposerInput(awaiting="branch_name", window_id="@5", repo="/tmp/x")
    gstate.arm(7, 100, ci)
    assert gstate.peek(7, 100) is ci
    assert gstate.disarm(7, 100) is ci
    assert gstate.peek(7, 100) is None


def test_state_threads_isolated() -> None:
    gstate.arm(7, 100, gstate.ComposerInput("a", "@5", "/r"))
    gstate.arm(7, 200, gstate.ComposerInput("b", "@6", "/r2"))
    assert gstate.peek(7, 100).window_id == "@5"
    assert gstate.peek(7, 200).window_id == "@6"


def test_one_line_truncates() -> None:
    long = "line one\n" + "x" * 1000
    out = gc._one_line(RuntimeError(long))
    assert "\n" not in out
    assert len(out) <= 300


def test_resolve_repo_prefers_workspace_path(monkeypatch) -> None:
    state.save(
        state.WindowSidecar(
            window_id="@5", window_creation_epoch=0.0, workspace_path="/ws/clone"
        )
    )
    assert gc._resolve_repo("@5") == "/ws/clone"


def test_resolve_repo_falls_back_to_cwd(monkeypatch) -> None:
    from dataclasses import dataclass

    @dataclass
    class _View:
        cwd: str

    monkeypatch.setattr(gc, "_resolve_repo", gc._resolve_repo)
    import ccgram.window_query as wq

    monkeypatch.setattr(wq, "view_window", lambda wid: _View(cwd="/proj"))
    # No sidecar with workspace_path → cwd from view.
    assert gc._resolve_repo("@nope") == "/proj"


def test_default_base_prefers_main(tmp_path, monkeypatch) -> None:
    # No origin/HEAD; main present in the branch list → picked.
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(
        ["git", "-C", str(repo), "init", "-q", "-b", "main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@e.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "T"],
        check=True,
        capture_output=True,
    )
    (repo / "f").write_text("x")
    subprocess.run(
        ["git", "-C", str(repo), "add", "f"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "i"], check=True, capture_output=True
    )
    assert gc._default_base(str(repo), ["dev", "main", "feat"]) == "main"

"""Tests for ``ccgram_pro.doctor`` — smoke + worst-status helpers."""

from __future__ import annotations

import pytest
from ccgram_pro import doctor


def test_use_colors_honors_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "")
    assert doctor._use_colors() is False


def test_use_colors_honors_force_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert doctor._use_colors() is True


def test_use_colors_no_color_wins_over_force(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert doctor._use_colors() is False


def test_worst_precedence() -> None:
    assert doctor._worst("OK", "OK", "OK") == "OK"
    assert doctor._worst("OK", "WARN", "OK") == "WARN"
    assert doctor._worst("WARN", "FAIL", "OK") == "FAIL"
    assert doctor._worst("FAIL") == "FAIL"


def test_run_doctor_returns_zero_when_everything_ok(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Phase 0 doctor only fails on missing entry points; everything else is OK/WARN."""
    rc = doctor.run_doctor()
    out = capsys.readouterr().out
    assert "ccgram-pro" in out
    assert "Overall:" in out
    assert rc == 0


def test_run_doctor_returns_one_when_entry_point_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If entry_points returns nothing, the doctor must FAIL."""

    def stub_entry_points(*_args, **kwargs):  # noqa: ANN001, ANN003
        # importlib.metadata.entry_points(group=...) returns an EntryPoints
        # which is iterable; an empty list satisfies the interface.
        del kwargs
        return []

    monkeypatch.setattr(doctor, "entry_points", stub_entry_points)
    rc = doctor.run_doctor()
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "missing" in out
    assert rc == 1


def test_check_dispatch_sites_detects_present(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = doctor._check_dispatch_sites()
    assert status == "OK"
    out = capsys.readouterr().out
    assert "dispatch_extensions" in out
    assert "_resolve_miniapp_factory" in out


def test_check_layer_dirs_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    status = doctor._check_layer_dirs()
    assert status == "OK"
    out = capsys.readouterr().out
    assert "writable" in out


def test_check_projects_warns_when_missing(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = doctor._check_projects(tmp_path / "nope.toml")
    assert status == "WARN"
    out = capsys.readouterr().out
    assert "missing" in out


def test_check_projects_ok_when_populated(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "projects.toml"
    f.write_text('[[project]]\npath = "/tmp/x"\nlabel = "X"\n')
    status = doctor._check_projects(f)
    assert status == "OK"
    out = capsys.readouterr().out
    assert "1 project" in out


def test_check_gh_cli_does_not_fail_when_absent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """gh is a Phase 7 dependency; Phase 0 doctor reports OK either way."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    status = doctor._check_gh_cli()
    assert status == "OK"
    out = capsys.readouterr().out
    assert "Phase 7" in out

"""Tests for ``ccgram_pro.config`` — TOML loading edge cases + defaults."""

from __future__ import annotations

from pathlib import Path

from ccgram_pro.config import (
    Defaults,
    Project,
    Settings,
    _coerce_bool,
    _coerce_int,
    _coerce_str,
    ensure_layer_dirs,
    layer_dir,
    load_projects,
    load_settings,
)


def test_layer_dir_default(tmp_path: Path) -> None:
    assert layer_dir() == tmp_path / "layer"


def test_layer_dir_namespaces_on_group_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CCGRAM_GROUP_ID", "humans")
    assert layer_dir() == tmp_path / "layer" / "group-humans"


def test_layer_dir_namespaces_on_instance_name_when_group_unset(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CCGRAM_INSTANCE_NAME", "bot-a")
    assert layer_dir() == tmp_path / "layer" / "instance-bot-a"


def test_layer_dir_prefers_group_over_instance(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CCGRAM_GROUP_ID", "g1")
    monkeypatch.setenv("CCGRAM_INSTANCE_NAME", "i1")
    assert layer_dir() == tmp_path / "layer" / "group-g1"


def test_ensure_layer_dirs_creates_all(tmp_path: Path) -> None:
    ensure_layer_dirs()
    base = tmp_path / "layer"
    assert (base / "state").is_dir()
    assert (base / "snapshots").is_dir()
    assert (base / "pr-loop").is_dir()


def test_load_projects_missing_returns_empty(tmp_path: Path) -> None:
    assert load_projects(tmp_path / "absent.toml") == []


def test_load_projects_happy(tmp_path: Path) -> None:
    f = tmp_path / "projects.toml"
    f.write_text(
        """
[[project]]
path = "/tmp/a"
label = "A"

[[project]]
path = "/tmp/b"
label = "B"
default_model = "sonnet"
default_reasoning = "high"
default_preamble = "be careful"
"""
    )
    projects = load_projects(f)
    assert len(projects) == 2
    assert projects[0] == Project(path=Path("/tmp/a"), label="A")
    assert projects[1].default_model == "sonnet"
    assert projects[1].default_reasoning == "high"
    assert projects[1].default_preamble == "be careful"


def test_load_projects_expands_user(tmp_path: Path) -> None:
    f = tmp_path / "projects.toml"
    f.write_text('[[project]]\npath = "~/foo"\nlabel = "F"\n')
    projects = load_projects(f)
    assert projects[0].path == Path.home() / "foo"


def test_load_projects_skips_malformed(tmp_path: Path) -> None:
    f = tmp_path / "projects.toml"
    f.write_text(
        """
[[project]]
label = "no path"

[[project]]
path = "/tmp/ok"
label = "ok"
"""
    )
    projects = load_projects(f)
    assert len(projects) == 1
    assert projects[0].label == "ok"


def test_load_projects_handles_toml_decode_error(tmp_path: Path) -> None:
    f = tmp_path / "projects.toml"
    f.write_text("[[ malformed toml")
    assert load_projects(f) == []


def test_load_projects_handles_non_array_project_key(tmp_path: Path) -> None:
    f = tmp_path / "projects.toml"
    f.write_text('project = "not an array"\n')
    assert load_projects(f) == []


def test_load_settings_missing_returns_defaults(tmp_path: Path) -> None:
    s = load_settings(tmp_path / "absent.toml")
    assert s == Settings()
    assert s.defaults.silent_mode is True
    assert s.defaults.batch_mode is True
    assert s.voice.flush_grace_seconds == 30


def test_load_settings_partial_overrides(tmp_path: Path) -> None:
    f = tmp_path / "settings.toml"
    f.write_text(
        """
[defaults]
silent_mode = false

[voice]
flush_grace_seconds = 5
"""
    )
    s = load_settings(f)
    assert s.defaults.silent_mode is False
    assert s.defaults.batch_mode is True  # still default
    assert s.voice.flush_grace_seconds == 5
    assert s.voice.transcription_note == Settings().voice.transcription_note


def test_load_settings_malformed_section_falls_back(tmp_path: Path) -> None:
    f = tmp_path / "settings.toml"
    f.write_text('defaults = "not a table"\n')
    s = load_settings(f)
    assert s.defaults == Defaults()


def test_load_settings_invalid_toml_returns_defaults(tmp_path: Path) -> None:
    f = tmp_path / "settings.toml"
    f.write_text(":::not toml at all")
    assert load_settings(f) == Settings()


def test_coerce_bool_only_accepts_real_booleans() -> None:
    assert _coerce_bool(True, False) is True
    assert _coerce_bool(False, True) is False
    assert _coerce_bool("yes", False) is False  # truthy string falls back
    assert _coerce_bool(1, False) is False  # ints fall back
    assert _coerce_bool(None, True) is True


def test_coerce_int_rejects_booleans() -> None:
    # isinstance(True, int) is True in Python; we MUST reject so a TOML
    # author writing ``flush_grace_seconds = true`` doesn't silently get 1.
    assert _coerce_int(True, 30) == 30
    assert _coerce_int(False, 30) == 30
    assert _coerce_int(5, 30) == 5
    assert _coerce_int("5", 30) == 30
    assert _coerce_int(None, 30) == 30


def test_coerce_str_only_accepts_strings() -> None:
    assert _coerce_str("hello", "x") == "hello"
    assert _coerce_str(5, "x") == "x"
    assert _coerce_str(None, "x") == "x"


def test_reactions_disabled_by_default() -> None:
    assert Defaults().reactions_enabled is False


def test_progress_bubble_enabled_by_default() -> None:
    assert Defaults().progress_bubble is True


def test_delete_transcript_on_teardown_off_by_default() -> None:
    assert Defaults().delete_transcript_on_teardown is False


def test_new_defaults_parse_from_toml(tmp_path: Path) -> None:
    f = tmp_path / "settings.toml"
    f.write_text(
        "[defaults]\n"
        "reactions_enabled = true\n"
        "progress_bubble = false\n"
        "delete_transcript_on_teardown = true\n"
    )
    settings = load_settings(f)
    assert settings.defaults.reactions_enabled is True
    assert settings.defaults.progress_bubble is False
    assert settings.defaults.delete_transcript_on_teardown is True

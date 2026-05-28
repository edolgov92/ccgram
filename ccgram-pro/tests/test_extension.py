"""Tests for ``ccgram_pro.extension`` and ``miniapp_factory`` entry-point targets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ccgram_pro import extension, miniapp_factory


def test_install_is_idempotent_and_creates_layer_dirs(tmp_path: Path) -> None:
    extension.install(None)  # type: ignore[arg-type]
    extension.install(None)  # second call must not raise
    assert (tmp_path / "layer" / "state").is_dir()
    assert (tmp_path / "layer" / "snapshots").is_dir()
    assert (tmp_path / "layer" / "pr-loop").is_dir()


def test_make_factory_passes_through_kwargs() -> None:
    captured: dict[str, Any] = {}

    def stub_build_app(**kwargs: Any) -> object:
        captured.update(kwargs)
        return "fake-app"

    factory = miniapp_factory.make_factory(stub_build_app)
    result = factory(bot_token="abc")
    assert result == "fake-app"
    assert captured == {"bot_token": "abc"}


def test_make_factory_forwards_arbitrary_keywords() -> None:
    """Future build_app params (terminal_capture etc.) must flow through."""
    captured: dict[str, Any] = {}

    def stub_build_app(**kwargs: Any) -> object:
        captured.update(kwargs)
        return "ok"

    factory = miniapp_factory.make_factory(stub_build_app)
    factory(bot_token="x", terminal_capture="cap", pane_list="pl")
    assert captured == {"bot_token": "x", "terminal_capture": "cap", "pane_list": "pl"}


def test_make_factory_surfaces_upstream_typeerror() -> None:
    """A renamed kwarg in upstream build_app must propagate as TypeError."""

    def picky_build_app(*, bot_token: str) -> object:  # noqa: ARG001
        return "ok"

    factory = miniapp_factory.make_factory(picky_build_app)
    with pytest.raises(TypeError):
        factory(unknown_kw="oops")

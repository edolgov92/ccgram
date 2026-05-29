"""Tests for ``ccgram_pro.extension`` and ``miniapp_factory`` entry-point targets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from ccgram.miniapp.server import _BOT_TOKEN_KEY  # type: ignore[attr-defined]
from ccgram_pro import extension, miniapp_factory


def test_install_is_idempotent_and_creates_layer_dirs(
    tmp_path: Path, monkeypatch
) -> None:
    """install() now wires JobQueue + CallbackQueryHandler against the
    application, so feed it a stub that records the calls.
    """
    handlers: list[object] = []
    jobs: list[str] = []

    class StubJobQueue:
        def run_repeating(self, _cb, **kw: Any) -> None:
            jobs.append(kw.get("name", "?"))

    class StubApplication:
        def __init__(self) -> None:
            self.job_queue = StubJobQueue()

        def add_handler(self, h: Any) -> None:
            handlers.append(h)

    # Avoid touching real ccgram modules from inside install_input_pipeline
    # by neutering the install guards on every wrapped subsystem.
    from ccgram_pro import handlers as layer_handlers_mod
    from ccgram_pro.input_pipeline import intercept as intercept_mod
    from ccgram_pro.output_pipeline import silencer as silencer_mod
    from ccgram_pro.output_pipeline import summarizer as summarizer_mod
    from ccgram_pro.plan_mode import orchestrator as plan_mode_mod

    intercept_mod._reset_for_testing()
    silencer_mod._reset_for_testing()
    summarizer_mod._reset_for_testing()
    plan_mode_mod._reset_for_testing()
    layer_handlers_mod._reset_for_testing()

    app = StubApplication()
    extension.install(app)  # type: ignore[arg-type]
    extension.install(app)  # second call must be idempotent
    assert (tmp_path / "layer" / "state").is_dir()
    assert (tmp_path / "layer" / "snapshots").is_dir()
    assert (tmp_path / "layer" / "pr-loop").is_dir()
    # JobQueue used (workspace GC) + at least one CallbackQueryHandler
    # registered (batch flush/clear).
    assert jobs and any("workspace_gc" in j for j in jobs)
    assert handlers


def _stub_build_app_factory(captured: dict[str, Any]):
    """Build a stub that captures kwargs but returns a real aiohttp Application
    so :func:`ccgram_pro.web.register_view_routes` (which `make_factory`
    now invokes) has something legal to attach routes to.
    """

    def stub_build_app(**kwargs: Any) -> web.Application:
        captured.update(kwargs)
        app = web.Application()
        app[_BOT_TOKEN_KEY] = kwargs.get("bot_token", "stub")
        return app

    return stub_build_app


def test_make_factory_passes_through_kwargs() -> None:
    captured: dict[str, Any] = {}
    factory = miniapp_factory.make_factory(_stub_build_app_factory(captured))
    result = factory(bot_token="abc")
    assert isinstance(result, web.Application)
    assert captured == {"bot_token": "abc"}
    # Layer route was registered on the returned app.
    assert any("/view/" in (r.resource.canonical or "") for r in result.router.routes())


def test_make_factory_forwards_arbitrary_keywords() -> None:
    """Future build_app params (terminal_capture etc.) must flow through."""
    captured: dict[str, Any] = {}
    factory = miniapp_factory.make_factory(_stub_build_app_factory(captured))
    factory(bot_token="x", terminal_capture="cap", pane_list="pl")
    assert captured == {"bot_token": "x", "terminal_capture": "cap", "pane_list": "pl"}


def test_make_factory_surfaces_upstream_typeerror() -> None:
    """A renamed kwarg in upstream build_app must propagate as TypeError."""

    def picky_build_app(*, bot_token: str) -> object:  # noqa: ARG001
        return "ok"

    factory = miniapp_factory.make_factory(picky_build_app)
    with pytest.raises(TypeError):
        factory(unknown_kw="oops")

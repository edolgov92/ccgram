"""Contract tests: ccgram's host symbols must exist and have stable shapes.

These are the upstream-pull canaries. If a future ccgram refactor renames
or restructures the entry-point dispatch sites, the layer breaks at
runtime. Catching it as a unit-test failure on `git pull` is much cheaper
than discovering it in production.
"""

from __future__ import annotations

import inspect


def test_bootstrap_dispatch_extensions_signature() -> None:
    from ccgram import bootstrap

    fn = getattr(bootstrap, "dispatch_extensions", None)
    assert fn is not None, "ccgram.bootstrap.dispatch_extensions missing"
    sig = inspect.signature(fn)
    # One positional arg (the PTB Application). Subsequent additions of
    # keyword-only params are forward-compatible.
    positional = [
        p
        for p in sig.parameters.values()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    assert len(positional) == 1


def test_bootstrap_application_calls_dispatch_extensions() -> None:
    """Sanity: the dispatch site exists inside bootstrap_application's body."""
    from ccgram import bootstrap

    source = inspect.getsource(bootstrap.bootstrap_application)
    assert "dispatch_extensions" in source


def test_bootstrap_reset_for_testing_resets_extensions_flag() -> None:
    """reset_for_testing must clear the _extensions_dispatched guard."""
    from ccgram import bootstrap

    source = inspect.getsource(bootstrap.reset_for_testing)
    assert "_extensions_dispatched" in source


def test_main_resolve_miniapp_factory_signature() -> None:
    from ccgram import main

    fn = getattr(main, "_resolve_miniapp_factory", None)
    assert fn is not None, "ccgram.main._resolve_miniapp_factory missing"
    sig = inspect.signature(fn)
    # No required parameters.
    required = [
        p
        for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    ]
    assert required == []


def test_main_start_miniapp_forwards_factory() -> None:
    """start_miniapp_if_enabled must thread the resolved factory into start_server."""
    from ccgram import main

    source = inspect.getsource(main.start_miniapp_if_enabled)
    assert "_resolve_miniapp_factory" in source
    assert "app_factory" in source


def test_miniapp_build_app_accepts_bot_token() -> None:
    """Our wrapper assumes build_app takes a keyword-only bot_token."""
    from ccgram.miniapp.server import build_app

    sig = inspect.signature(build_app)
    bot_token = sig.parameters.get("bot_token")
    assert bot_token is not None
    assert bot_token.kind == inspect.Parameter.KEYWORD_ONLY

"""ccgram.miniapp_factory entry-point target — wraps build_app + adds routes.

Called once at startup by :func:`ccgram.main._resolve_miniapp_factory`
with ccgram's default ``build_app`` callable. Returns a new factory;
when invoked by ``start_server`` the wrapper builds the base app then
registers the layer's aiohttp routes (currently: the ``/view/{token}``
share-link viewer; future phases add diff + PR routes).

Wrapper limitations:

- New aiohttp routes may be **added** to the returned ``Application``;
  mutating or replacing upstream's existing routes is not supported.
- ``start_server`` currently only forwards ``bot_token`` to ``build_app``
  (see ``ccgram.miniapp.server.start_server``); the other test-injection
  ``build_app`` parameters cannot be reached through the entry-point
  seam.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from . import __version__
from .web import register_diff_routes, register_view_routes

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiohttp import web

logger = structlog.get_logger()


def make_factory(
    default_build_app: Callable[..., web.Application],
) -> Callable[..., web.Application]:
    """Return a wrapping factory that delegates to *default_build_app*.

    The returned factory accepts the same keyword arguments as upstream
    ``build_app`` and forwards them verbatim. After the base app is
    built, layer-owned routes are registered.
    """

    def factory(**kwargs: object) -> web.Application:
        app = default_build_app(**kwargs)
        register_view_routes(app)
        register_diff_routes(app)
        logger.debug("ccgram-pro %s miniapp factory built app", __version__)
        return app

    return factory

"""ccgram.miniapp_factory entry-point target — wraps build_app.

Called once at startup by :func:`ccgram.main._resolve_miniapp_factory` with
ccgram's default ``build_app`` callable. Returns a new factory; when invoked
by ``start_server`` the wrapper builds the base app then registers
layer-owned routes (diff viewer, long-summary viewer, share links — added
in Phase 5+).

Phase 0 leaves the wrapper a thin pass-through so the call signature is
exercised end-to-end. Adding routes in later phases is a non-breaking
change to this file.

Wrapper limitations:

- New aiohttp routes may be **added** to the returned ``Application`` via
  ``app.router.add_*``. Mutating or replacing upstream's existing routes
  is not supported (aiohttp's ``UrlDispatcher`` exposes no clean override
  API); cross-cutting needs go through ``app.middlewares.append(...)``
  instead.
- ``start_server`` currently only forwards ``bot_token`` to ``build_app``
  (see ``ccgram.miniapp.server.start_server``). The other ``build_app``
  parameters (``terminal_capture``, ``pane_capture`` etc.) are
  test-injection points; a production wrapper cannot reach them through
  the entry-point seam.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from . import __version__

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiohttp import web

logger = structlog.get_logger()


def make_factory(
    default_build_app: Callable[..., web.Application],
) -> Callable[..., web.Application]:
    """Return a wrapping factory that delegates to *default_build_app*.

    The returned factory accepts the same keyword arguments as upstream
    ``build_app`` and forwards them verbatim. A renamed kwarg on the
    upstream side will surface as a ``TypeError`` from
    ``default_build_app(**kwargs)`` at startup — caught and logged by
    :func:`ccgram.main._resolve_miniapp_factory`, so the bot still starts
    with the un-wrapped factory.
    """

    def factory(**kwargs: object) -> web.Application:
        app = default_build_app(**kwargs)
        logger.debug("ccgram-pro %s miniapp factory built app", __version__)
        return app

    return factory

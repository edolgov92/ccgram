"""aiohttp routes registered on the Mini App server by ccgram-pro.

Module-level so the miniapp_factory entry-point target can register the
routes alongside the upstream ones in a single :func:`build_app`
invocation.
"""

from .routes_view import register_view_routes

__all__ = ["register_view_routes"]

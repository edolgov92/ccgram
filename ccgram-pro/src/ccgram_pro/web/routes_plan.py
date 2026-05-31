"""``GET /plan/{token}`` — render an ExitPlanMode plan as a clean HTML page.

Telegram shows only the condensed main idea; this page renders the full plan
markdown (headings, lists, code) for the user who taps "📄 View full plan".
Token auth mirrors ``/view`` (HMAC share token); only ``kind == "plan"`` records
render here, so a transcript token can't be redirected to this surface.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

import structlog

from ccgram.miniapp.server import _BOT_TOKEN_KEY  # type: ignore[attr-defined]

from ..share.links import resolve_token
from ..share.store import ShareNotFound
from ..share.tokens import InvalidShareToken
from ._page_shell import render_page
from .transcript_render import plan_css, render_plan_markdown

if TYPE_CHECKING:
    from aiohttp import web

logger = structlog.get_logger()


async def _handle_plan(request: "web.Request") -> "web.Response":
    # Lazy: aiohttp only needed inside the request handler.
    from aiohttp import web

    token = request.match_info.get("token", "")
    bot_token = request.app[_BOT_TOKEN_KEY]
    try:
        record = resolve_token(token, bot_token=bot_token)
    except InvalidShareToken as exc:
        logger.debug("plan view rejected: %s", exc)
        return web.Response(status=403, text="invalid or expired link")
    except ShareNotFound:
        return web.Response(status=404, text="plan not found")

    if record.kind != "plan":
        return web.Response(status=404, text="not a plan")

    body = (
        "<h1>📋 Implementation plan</h1>"
        f'<article class="plan">{render_plan_markdown(record.body_markdown)}</article>'
    )
    page = render_page(
        title=html.escape(record.title or "Plan"),
        body_html=body,
        footer="ccgram-pro · plan · link expires 3 days from issue",
        extra_css=plan_css(),
    )
    return web.Response(text=page, content_type="text/html")


def register_plan_routes(app: "web.Application") -> None:
    """Register the ``GET /plan/{token}`` route."""
    app.router.add_get("/plan/{token}", _handle_plan)
    logger.debug("ccgram-pro plan route registered: GET /plan/{token}")

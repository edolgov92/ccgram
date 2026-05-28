"""``GET /view/{token}`` — render a share as a clean HTML page.

The page intentionally has no inline JavaScript or external CDN — the
mobile audience for these links benefits from instant first paint, and a
read-only markdown render does not need DOM interactivity. CSS lives
inline so the page works behind any reverse proxy without caching
headaches.

XSS posture: every share field is run through ``html.escape``. Markdown
rendering is deliberately minimal (fenced code blocks → ``<pre><code>``,
inline backticks → ``<code>``) so we never have to vet a markdown
library against malicious content authored by Claude. Future richer
rendering can swap in a battle-tested parser; until then, plain ``<pre>``
for code is the safe default.
"""

from __future__ import annotations

import datetime as _dt
import html
import re
from typing import TYPE_CHECKING

import structlog

from ccgram.miniapp.server import _BOT_TOKEN_KEY  # type: ignore[attr-defined]

from ..share.links import resolve_token
from ..share.tokens import InvalidShareToken
from ..share.store import ShareNotFound

if TYPE_CHECKING:
    from aiohttp import web

logger = structlog.get_logger()


def _render_markdown(body: str) -> str:
    """Tiny markdown-to-HTML renderer — escapes everything, then re-injects
    a small set of safe block patterns.

    Supported:
    - Fenced code blocks ``` … ``` → ``<pre><code>…</code></pre>``
    - Inline backticks ``…`` → ``<code>…</code>``
    - Plain paragraphs split on blank lines

    Anything else is shown verbatim. That's fine for the long-view use
    case — the source content comes from Claude transcripts which are
    text, not authored HTML.
    """
    escaped = html.escape(body)

    def _fenced(match: "re.Match[str]") -> str:
        return f"<pre><code>{match.group(1)}</code></pre>"

    escaped = re.sub(r"```([\s\S]*?)```", _fenced, escaped)
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)

    paragraphs = [
        f"<p>{para.replace(chr(10), '<br>')}</p>"
        for para in re.split(r"\n\s*\n", escaped.strip())
        if para.strip()
    ]
    return "\n".join(paragraphs) or "<p><em>(empty)</em></p>"


_PAGE_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>{title}</title>
<style>
  :root {{
    color-scheme: dark light;
    --bg: #0d1117;
    --fg: #e6edf3;
    --muted: #8b949e;
    --accent: #58a6ff;
    --code-bg: #161b22;
    --border: #30363d;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: var(--bg); color: var(--fg);
              font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  main {{ max-width: 760px; margin: 0 auto; padding: 24px 18px 80px; }}
  header {{ border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 6px; word-break: break-word; }}
  .meta {{ color: var(--muted); font-size: 0.85rem; }}
  .meta a {{ color: var(--accent); text-decoration: none; }}
  article p {{ margin: 0 0 1em; word-wrap: break-word; }}
  article code {{ background: var(--code-bg); padding: 1px 6px; border-radius: 4px;
                font-family: "SF Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
                font-size: 0.92em; }}
  article pre {{ background: var(--code-bg); padding: 12px 14px; border-radius: 8px;
                overflow-x: auto; border: 1px solid var(--border); margin: 0 0 1em; }}
  article pre code {{ background: none; padding: 0; font-size: 0.9em; }}
  footer {{ margin-top: 40px; padding-top: 16px; border-top: 1px solid var(--border);
            color: var(--muted); font-size: 0.78rem; }}
</style>
</head>
<body>
<main>
  <header>
    <h1>{title}</h1>
    <div class="meta">Kind: <code>{kind}</code> · Created {created} · Window: <code>{window_id}</code></div>
  </header>
  <article>
    {body_html}
  </article>
  <footer>ccgram-pro share · token expires 3 days from issue · refresh to revoke caching</footer>
</main>
</body>
</html>
"""


def _format_created(epoch: float) -> str:
    if not epoch:
        return "unknown"
    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


async def _handle_view(request: "web.Request") -> "web.Response":
    from aiohttp import web

    token = request.match_info.get("token", "")
    bot_token = request.app[_BOT_TOKEN_KEY]

    try:
        record = resolve_token(token, bot_token=bot_token)
    except InvalidShareToken as exc:
        logger.debug("view rejected: %s", exc)
        return web.Response(status=403, text="invalid or expired link")
    except ShareNotFound:
        return web.Response(status=404, text="share not found")

    page = _PAGE_TEMPLATE.format(
        title=html.escape(record.title or "ccgram share"),
        kind=html.escape(record.kind),
        created=html.escape(_format_created(record.created_at)),
        window_id=html.escape(record.window_id or "—"),
        body_html=_render_markdown(record.body_markdown),
    )
    return web.Response(text=page, content_type="text/html")


def register_view_routes(app: "web.Application") -> None:
    """Register the ``/view/{token}`` route on the aiohttp app."""
    app.router.add_get("/view/{token}", _handle_view)
    logger.debug("ccgram-pro view route registered: GET /view/{token}")

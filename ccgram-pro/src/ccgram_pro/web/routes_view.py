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
import json
import re
from typing import TYPE_CHECKING

import structlog

from ccgram.miniapp.server import _BOT_TOKEN_KEY  # type: ignore[attr-defined]

from ..output_pipeline.transcript_events import events_from_dicts
from ..share.links import resolve_token
from ..share.store import ShareNotFound
from ..share.tokens import InvalidShareToken
from .transcript_render import render_rows_html, transcript_css

if TYPE_CHECKING:
    from aiohttp import web

logger = structlog.get_logger()

# Pagination: render the newest page first; older events load on
# scroll-to-top (JS) or via the no-JS "Load older" link.
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


def _parse_event_dicts(body: str) -> list[dict] | None:
    """Return the stored event dicts if *body* is a v2 transcript envelope.

    Legacy/markdown shares return ``None`` so the caller falls back to the
    markdown renderer.
    """
    stripped = body.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and isinstance(data.get("events"), list):
        return data["events"]
    return None


def _clamp_limit(raw: str | None) -> int:
    try:
        value = int(raw) if raw is not None else _DEFAULT_LIMIT
    except TypeError, ValueError:
        return _DEFAULT_LIMIT
    return max(1, min(value, _MAX_LIMIT))


def _window(
    event_dicts: list[dict], *, before: int | None, limit: int
) -> tuple[list[dict], int]:
    """Slice the newest *limit* events ending at *before* (exclusive).

    Returns ``(window, start_index)`` where ``start_index`` is the index
    of the first event in the window (0 means we've reached the oldest —
    nothing more to load). ``before=None`` means "from the very end".
    """
    end = len(event_dicts) if before is None else max(0, min(before, len(event_dicts)))
    start = max(0, end - limit)
    return event_dicts[start:end], start


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
{transcript_css}
  .load-older {{ display: block; text-align: center; padding: 10px; margin-bottom: 8px;
                 color: var(--accent); text-decoration: none; font-size: 0.85rem;
                 border: 1px dashed var(--border); border-radius: 8px; }}
  .load-older:hover {{ background: var(--code-bg); }}
  .load-older.done {{ color: var(--muted); border-style: solid; pointer-events: none; }}
</style>
</head>
<body>
<main>
  <header>
    <h1>{title}</h1>
    <div class="meta">Kind: <code>{kind}</code> · Created {created} · Window: <code>{window_id}</code> · {total} message(s)</div>
  </header>
  {older_link}
  <div class="transcript" id="transcript">{rows_html}</div>
  <footer>ccgram-pro share · token expires 3 days from issue</footer>
</main>
{infinite_scroll_js}
</body>
</html>
"""

_INFINITE_SCROLL_JS = """\
<script>
(function () {
  var sentinel = document.getElementById('load-older');
  var container = document.getElementById('transcript');
  if (!sentinel || !container) return;
  var token = sentinel.dataset.token;
  var oldest = parseInt(sentinel.dataset.oldest, 10);
  var limit = parseInt(sentinel.dataset.limit, 10);
  var loading = false;
  var done = oldest <= 0;

  function finish() {
    done = true;
    sentinel.classList.add('done');
    sentinel.textContent = '— start of conversation —';
  }

  async function loadOlder() {
    if (loading || done) return;
    loading = true;
    sentinel.textContent = 'Loading older…';
    try {
      var r = await fetch('/view/' + token + '/older?before=' + oldest + '&limit=' + limit);
      if (!r.ok) { finish(); return; }
      var data = await r.json();
      if (!data.rows) { finish(); return; }
      var prevHeight = document.documentElement.scrollHeight;
      container.insertAdjacentHTML('afterbegin', data.rows);
      // Preserve the reading position when content is prepended above.
      var added = document.documentElement.scrollHeight - prevHeight;
      window.scrollBy(0, added);
      oldest = data.oldest;
      sentinel.dataset.oldest = String(oldest);
      sentinel.setAttribute('href', '?before=' + oldest + '&limit=' + limit);
      if (oldest <= 0) { finish(); }
      else { sentinel.textContent = '↑ Load older messages'; }
    } catch (e) {
      sentinel.textContent = '↑ Load older messages';
    } finally {
      loading = false;
    }
  }

  // Click works everywhere; the href is a no-JS fallback that this
  // handler intercepts when JS is on.
  sentinel.addEventListener('click', function (ev) {
    ev.preventDefault();
    loadOlder();
  });
  // Auto-load when the sentinel scrolls into view.
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(function (entries) {
      if (entries[0].isIntersecting) loadOlder();
    }, { rootMargin: '300px' }).observe(sentinel);
  }
})();
</script>
"""


def _format_created(epoch: float) -> str:
    if not epoch:
        return "unknown"
    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def _older_link_html(*, token: str, oldest_index: int, limit: int) -> str:
    """Build the top "Load older" sentinel (also the no-JS pagination link).

    ``oldest_index`` is the index of the first event currently shown; when
    it is 0 we're already at the start and render nothing.
    """
    if oldest_index <= 0:
        return ""
    href = f"?before={oldest_index}&amp;limit={limit}"
    return (
        f'<a id="load-older" class="load-older" href="{href}" '
        f'data-token="{html.escape(token, quote=True)}" '
        f'data-oldest="{oldest_index}" data-limit="{limit}">'
        "↑ Load older messages</a>"
    )


async def _handle_view(request: "web.Request") -> "web.Response":
    # Lazy: aiohttp only needed inside the request handler.
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

    event_dicts = _parse_event_dicts(record.body_markdown)
    common = {
        "title": html.escape(record.title or "ccgram share"),
        "kind": html.escape(record.kind),
        "created": html.escape(_format_created(record.created_at)),
        "window_id": html.escape(record.window_id or "—"),
        "transcript_css": transcript_css(),
    }

    if event_dicts is None:
        # Legacy / non-transcript share → render markdown, no pagination.
        page = _PAGE_TEMPLATE.format(
            **common,
            total=1,
            older_link="",
            rows_html=f"<article>{_render_markdown(record.body_markdown)}</article>",
            infinite_scroll_js="",
        )
        return web.Response(text=page, content_type="text/html")

    total = len(event_dicts)
    limit = _clamp_limit(request.query.get("limit"))
    before_raw = request.query.get("before")
    before = None
    if before_raw is not None:
        try:
            before = int(before_raw)
        except ValueError:
            before = None
    window, start_index = _window(event_dicts, before=before, limit=limit)
    rows = render_rows_html(events_from_dicts(window))

    page = _PAGE_TEMPLATE.format(
        **common,
        total=total,
        older_link=_older_link_html(token=token, oldest_index=start_index, limit=limit),
        rows_html=rows or '<p class="empty">No transcript content.</p>',
        infinite_scroll_js=_INFINITE_SCROLL_JS if start_index > 0 else "",
    )
    return web.Response(text=page, content_type="text/html")


async def _handle_view_older(request: "web.Request") -> "web.Response":
    """JSON fragment endpoint for infinite scroll.

    Returns ``{"rows": "<html>", "oldest": <int>}`` for the events
    immediately before ``?before``. ``oldest`` is the new first-shown
    index (0 means the client has reached the start).
    """
    # Lazy: aiohttp only needed inside the request handler.
    from aiohttp import web

    token = request.match_info.get("token", "")
    bot_token = request.app[_BOT_TOKEN_KEY]
    try:
        record = resolve_token(token, bot_token=bot_token)
    except InvalidShareToken:
        return web.json_response({"error": "invalid"}, status=403)
    except ShareNotFound:
        return web.json_response({"error": "not found"}, status=404)

    event_dicts = _parse_event_dicts(record.body_markdown)
    if event_dicts is None:
        return web.json_response({"rows": "", "oldest": 0})

    limit = _clamp_limit(request.query.get("limit"))
    try:
        before = int(request.query.get("before", len(event_dicts)))
    except ValueError:
        before = len(event_dicts)
    window, start_index = _window(event_dicts, before=before, limit=limit)
    rows = render_rows_html(events_from_dicts(window))
    return web.json_response({"rows": rows, "oldest": start_index})


def register_view_routes(app: "web.Application") -> None:
    """Register the ``/view/{token}`` + older-fragment routes."""
    app.router.add_get("/view/{token}", _handle_view)
    app.router.add_get("/view/{token}/older", _handle_view_older)
    logger.debug(
        "ccgram-pro view routes registered: GET /view/{token} (+ /older fragment)"
    )

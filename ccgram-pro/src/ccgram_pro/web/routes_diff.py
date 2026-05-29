"""``GET /diff/{token}`` — render a window's diff snapshot as HTML.

Toggle ``?anchor=session|iteration`` switches between the two anchors
the snapshot writer maintains. Defaults to ``iteration`` (= "what
changed in Claude's last turn") because that's the question the user
asks most often.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

import structlog

from ccgram.miniapp.server import _BOT_TOKEN_KEY  # type: ignore[attr-defined]

from ..git_ops.diff import parse_unified_diff
from ..git_ops.snapshot import SnapshotNotFound, list_snapshots, load_snapshot
from ..share.tokens import InvalidShareToken, verify_share_token
from .diff_render import diff_page_css, render_diff_html

if TYPE_CHECKING:
    from aiohttp import web

logger = structlog.get_logger()


_PAGE_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>Diff · {window_id}</title>
<style>
  :root {{
    color-scheme: dark;
    --bg: #0a0c10;
    --bg-grad: radial-gradient(1200px 600px at 50% -10%, #141926 0%, #0a0c10 60%);
    --surface: #12151d;
    --elevated: #171b25;
    --fg: #eceef4;
    --muted: #99a1b3;
    --faint: #6b7280;
    --accent: #8aa6ff;
    --border: #232936;
    --border-soft: #1b212c;
    --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.22);
    --font: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto,
            "Helvetica Neue", Arial, "Inter", sans-serif;
    --mono: ui-monospace, "SF Mono", "JetBrains Mono", "Cascadia Code",
            Menlo, Consolas, monospace;
  }}
  * {{ box-sizing: border-box; }}
  html {{ -webkit-text-size-adjust: 100%; }}
  body {{ margin: 0; background: var(--bg); background-image: var(--bg-grad);
          background-attachment: fixed; color: var(--fg); font-family: var(--font);
          font-size: 15.5px; line-height: 1.6; -webkit-font-smoothing: antialiased; }}
  main {{ max-width: 980px; margin: 0 auto; padding: 0 16px 96px; }}
  header.page {{ position: sticky; top: 0; z-index: 10; margin: 0 -16px 18px;
            padding: 16px 16px 14px; background: rgba(10,12,16,.72);
            backdrop-filter: saturate(140%) blur(14px);
            -webkit-backdrop-filter: saturate(140%) blur(14px);
            border-bottom: 1px solid var(--border-soft); }}
  h1 {{ font-size: 1.14rem; font-weight: 650; letter-spacing: -0.01em; margin: 0 0 8px;
        word-break: break-all;
        background: linear-gradient(92deg, var(--fg), #c7cede);
        -webkit-background-clip: text; background-clip: text;
        -webkit-text-fill-color: transparent; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }}
  .meta .chip {{ font-size: 0.72rem; color: var(--muted); background: var(--surface);
                 border: 1px solid var(--border-soft); border-radius: 999px;
                 padding: 3px 10px; white-space: nowrap; }}
  .meta .chip code {{ color: var(--accent); font-family: var(--mono); font-size: 0.92em; }}
  .toggle {{ display: inline-flex; gap: 4px; padding: 4px; background: var(--surface);
            border: 1px solid var(--border-soft); border-radius: 12px; }}
  .toggle a {{ padding: 7px 16px; text-decoration: none; color: var(--muted);
              border-radius: 9px; font-size: 0.82rem; font-weight: 550;
              transition: background .12s ease, color .12s ease; }}
  .toggle a:hover {{ color: var(--fg); }}
  .toggle a.active {{ background: linear-gradient(140deg, #6d8bff, #b69cff);
                      color: #0b0d12; font-weight: 650; box-shadow: var(--shadow); }}
  footer {{ margin-top: 44px; padding-top: 18px; border-top: 1px solid var(--border-soft);
            color: var(--faint); font-size: 0.76rem; text-align: center; }}
{diff_css}
</style>
</head>
<body>
<main>
  <header class="page">
    <h1>{title}</h1>
    <div class="meta">{meta_line}</div>
    <div class="toggle">{toggle_html}</div>
  </header>
  <div class="diff">{diff_html}</div>
  <footer>ccgram-pro · diff snapshot token expires 3 days from issue</footer>
</main>
</body>
</html>
"""


_VALID_ANCHORS = ("iteration", "session")


def _build_toggle(*, token: str, current: str, available: list[str]) -> str:
    items: list[str] = []
    for anchor in _VALID_ANCHORS:
        if anchor not in available:
            continue
        label = "Last iteration" if anchor == "iteration" else "Since session start"
        cls = "active" if anchor == current else ""
        items.append(
            f'<a href="/diff/{html.escape(token)}?anchor={anchor}" class="{cls}">{label}</a>'
        )
    return "\n".join(items) or "<span></span>"


async def _handle_diff(request: "web.Request") -> "web.Response":
    # Lazy: aiohttp only needed inside the request handler.
    from aiohttp import web

    token = request.match_info.get("token", "")
    bot_token = request.app[_BOT_TOKEN_KEY]

    try:
        payload = verify_share_token(token, bot_token=bot_token)
    except InvalidShareToken as exc:
        logger.debug("diff token rejected: %s", exc)
        return web.Response(status=403, text="invalid or expired link")

    # Token's share_id field reuses the window_id encoding so a single
    # signer can mint both view-share and diff-share tokens against
    # the same store. The diff route doesn't touch the share store —
    # it loads the diff snapshot directly.
    window_id = payload.share_id

    anchor = request.query.get("anchor", "iteration")
    if anchor not in _VALID_ANCHORS:
        anchor = "iteration"

    available = list_snapshots(window_id)
    if not available:
        return web.Response(
            status=404,
            text="No diff snapshots for this window yet.",
        )
    if anchor not in available:
        anchor = available[0]

    try:
        snapshot = load_snapshot(window_id, anchor)
    except SnapshotNotFound:
        return web.Response(status=404, text="snapshot missing")

    files = parse_unified_diff(snapshot.diff_text)
    diff_html = render_diff_html(
        files,
        empty_message=(
            "No changes since session start."
            if anchor == "session"
            else "No changes in Claude's last iteration."
        ),
    )

    meta_line = (
        f'<span class="chip">🌿 <code>{html.escape(snapshot.branch)}</code></span>'
        f'<span class="chip">⎇ <code>{html.escape(snapshot.head_sha[:12])}</code></span>'
        f'<span class="chip">📂 <code>{html.escape(snapshot.project_root)}</code></span>'
    )
    title = html.escape(f"Diff · {window_id} · {anchor}")
    page = _PAGE_TEMPLATE.format(
        window_id=html.escape(window_id),
        title=title,
        meta_line=meta_line,
        toggle_html=_build_toggle(token=token, current=anchor, available=available),
        diff_html=diff_html,
        diff_css=diff_page_css(),
    )
    return web.Response(text=page, content_type="text/html")


def register_diff_routes(app: "web.Application") -> None:
    """Register ``GET /diff/{token}`` on the aiohttp app."""
    app.router.add_get("/diff/{token}", _handle_diff)
    logger.debug("ccgram-pro diff route registered: GET /diff/{token}")

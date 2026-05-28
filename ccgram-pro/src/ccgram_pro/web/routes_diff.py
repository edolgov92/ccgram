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
  main {{ max-width: 960px; margin: 0 auto; padding: 18px; }}
  header.page {{ border-bottom: 1px solid var(--border); padding-bottom: 12px; margin-bottom: 16px; }}
  h1 {{ font-size: 1.2rem; margin: 0 0 6px; word-break: break-all; }}
  .meta {{ color: var(--muted); font-size: 0.85rem; }}
  .toggle {{ display: inline-flex; gap: 0; margin: 10px 0; border: 1px solid var(--border);
            border-radius: 8px; overflow: hidden; }}
  .toggle a {{ padding: 6px 14px; text-decoration: none; color: var(--fg);
              background: var(--code-bg); font-size: 0.85rem; }}
  .toggle a.active {{ background: var(--accent); color: var(--bg); font-weight: 600; }}
  footer {{ margin-top: 40px; padding-top: 16px; border-top: 1px solid var(--border);
            color: var(--muted); font-size: 0.78rem; }}
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
        label = (
            "Last iteration" if anchor == "iteration" else "Since session start"
        )
        cls = "active" if anchor == current else ""
        items.append(
            f'<a href="/diff/{html.escape(token)}?anchor={anchor}" class="{cls}">{label}</a>'
        )
    return "\n".join(items) or "<span></span>"


async def _handle_diff(request: "web.Request") -> "web.Response":
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
        empty_message=("No changes since session start." if anchor == "session"
                       else "No changes in Claude's last iteration."),
    )

    meta_line = (
        f"Branch: <code>{html.escape(snapshot.branch)}</code> · "
        f"Anchor sha: <code>{html.escape(snapshot.head_sha[:12])}</code> · "
        f"Project: <code>{html.escape(snapshot.project_root)}</code>"
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

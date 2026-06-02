"""``GET /diff/{token}`` — render a window's diff between frozen snapshots.

``?anchor=iteration`` (default) shows ``diff(snap[N-1], snap[N])`` — Claude's
last turn. ``?anchor=session`` shows ``diff(snap[0], snap[N])`` — everything
since the session started. Both are computed from immutable snapshot commits so
they survive commits / pushes / branch switches.

``GET /diff/{token}/expand`` is the JSON companion the page's expander buttons
call to pull more unchanged context lines from a snapshot's file content.
"""

from __future__ import annotations

import html
import json
from typing import TYPE_CHECKING

import structlog

from ccgram.miniapp.server import _BOT_TOKEN_KEY  # type: ignore[attr-defined]

from ..git_ops.diff import parse_unified_diff
from ..git_ops.snapshot import (
    diff_between,
    file_content_at,
    load_index,
    session_base_n,
)
from ..share.tokens import InvalidShareToken, verify_share_token
from ._page_shell import render_page
from .diff_render import diff_css, diff_js, render_diff_files

if TYPE_CHECKING:
    from aiohttp import web

logger = structlog.get_logger()

_VALID_ANCHORS = ("iteration", "session")
_MAX_EXPAND_COUNT = 200


def _anchor_range(index, anchor: str) -> tuple[int, int]:
    """Map an anchor to ``(base_n, target_n)`` snapshot indices.

    ``session`` anchors to the earliest snapshot on the *current* branch (via
    :func:`session_base_n`), not the literal ``n=0`` — so a mid-session branch
    switch never leaks the inter-branch delta into the "since session start"
    diff. ``iteration`` is the previous snapshot → the latest.
    """
    latest = index.entries[-1].n
    if anchor == "session":
        return session_base_n(index), latest
    return max(latest - 1, 0), latest


def _toggle_html(*, token: str, current: str) -> str:
    items: list[str] = []
    for anchor in _VALID_ANCHORS:
        label = "Last iteration" if anchor == "iteration" else "Since session start"
        cls = "active" if anchor == current else ""
        items.append(
            f'<a href="/diff/{html.escape(token)}?anchor={anchor}" class="{cls}">{label}</a>'
        )
    return "".join(items)


def _meta_chips(index, target_n: int) -> str:
    entry = next((e for e in index.entries if e.n == target_n), None)
    branch = entry.branch if entry else ""
    head = entry.real_head_sha[:12] if entry and entry.real_head_sha else "—"
    chips = [
        f'<span class="chip">🌿 <code>{html.escape(branch or "—")}</code></span>',
        f'<span class="chip">⎇ <code>{html.escape(head)}</code></span>',
        f'<span class="chip">📂 <code>{html.escape(index.project_root)}</code></span>',
    ]
    return "".join(chips)


_TOGGLE_CSS = """
  .meta { display:flex; flex-wrap:wrap; gap:6px; margin:0 0 12px; }
  .chip { font-size:0.72rem; color:var(--muted); background:var(--surface);
          border:1px solid var(--border-soft); border-radius:999px; padding:3px 10px; }
  .chip code { color:var(--accent); font-family:var(--mono); }
  .toggle { display:inline-flex; gap:4px; padding:4px; background:var(--surface);
            border:1px solid var(--border-soft); border-radius:12px; margin-bottom:18px; }
  .toggle a { padding:7px 16px; text-decoration:none; color:var(--muted);
              border-radius:9px; font-size:0.82rem; font-weight:550; }
  .toggle a.active { background:linear-gradient(140deg,#6d8bff,#b69cff);
              color:#0b0d12; font-weight:650; }
"""


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

    window_id = payload.share_id
    index = load_index(window_id)
    if index is None or not index.entries:
        return web.Response(status=404, text="No diff snapshots for this window yet.")

    anchor = request.query.get("anchor", "iteration")
    if anchor not in _VALID_ANCHORS:
        anchor = "iteration"
    base_n, target_n = _anchor_range(index, anchor)

    raw = diff_between(window_id, base_n=base_n, target_n=target_n)
    files = parse_unified_diff(raw)
    empty = (
        "No changes since session start."
        if anchor == "session"
        else "No code changes in Claude's last turn — switch to “Since session start”."
    )
    body = (
        f"<h1>📊 Diff · {html.escape(window_id)}</h1>"
        f'<div class="meta">{_meta_chips(index, target_n)}</div>'
        f'<div class="toggle">{_toggle_html(token=token, current=anchor)}</div>'
        '<div class="opts"><button type="button" id="lnBtn" class="opt" '
        'onclick="ccgToggleLn()"># Hide line numbers</button></div>'
        f'<div class="diff">{render_diff_files(files, empty_message=empty)}</div>'
        f"<script>const DIFF_TOKEN={_js_str(token)};"
        f"const DIFF_ANCHOR={_js_str(anchor)};{diff_js()}</script>"
    )
    page = render_page(
        title=f"Diff · {window_id}",
        body_html=body,
        footer="ccgram-pro · diff snapshot · link expires 3 days from issue",
        extra_css=_TOGGLE_CSS + diff_css(),
    )
    return web.Response(text=page, content_type="text/html")


def _js_str(value: str) -> str:
    """JSON-encode a string for safe inline-script embedding."""
    return json.dumps(value)


def _sanitize_rel_path(path: str) -> str | None:
    if not path or path.startswith("/") or ".." in path.split("/"):
        return None
    return path


async def _handle_diff_expand(request: "web.Request") -> "web.Response":
    # Lazy: aiohttp only needed inside the request handler.
    from aiohttp import web

    token = request.match_info.get("token", "")
    bot_token = request.app[_BOT_TOKEN_KEY]
    try:
        payload = verify_share_token(token, bot_token=bot_token)
    except InvalidShareToken:
        return web.json_response({"error": "invalid"}, status=403)

    window_id = payload.share_id
    index = load_index(window_id)
    if index is None or not index.entries:
        return web.json_response({"lines": []})

    path = _sanitize_rel_path(request.query.get("path", ""))
    if path is None:
        return web.json_response({"error": "bad path"}, status=400)

    anchor = request.query.get("anchor", "iteration")
    if anchor not in _VALID_ANCHORS:
        anchor = "iteration"
    _base_n, target_n = _anchor_range(index, anchor)

    try:
        start = max(int(request.query.get("start", "1")), 1)
        count = max(min(int(request.query.get("count", "40")), _MAX_EXPAND_COUNT), 1)
    except ValueError:
        return web.json_response({"error": "bad range"}, status=400)

    content = file_content_at(window_id, n=target_n, path=path)
    if content is None:
        return web.json_response({"lines": []})
    all_lines = content.split("\n")
    # 1-indexed new-side line numbers → 0-indexed slice.
    window = all_lines[start - 1 : start - 1 + count]
    return web.json_response({"lines": window})


def register_diff_routes(app: "web.Application") -> None:
    """Register the diff page + context-expand JSON endpoint."""
    app.router.add_get("/diff/{token}", _handle_diff)
    app.router.add_get("/diff/{token}/expand", _handle_diff_expand)
    logger.debug("ccgram-pro diff routes registered: GET /diff/{token} (+ /expand)")

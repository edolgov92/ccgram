"""``GET /live/{token}`` — real-time activity view for one window.

Unlike ``/view`` (an immutable snapshot of a finished turn), this is a live
window into what the agent is doing *right now*. Three surfaces, in one page:

1. A **status banner** — Working / Awaiting your answer / Idle, plus a flag when
   the agent is idle-but-a-screen-is-open (the ``/usage``-modal stuck case).
2. A **live structured stream** — new reasoning / tool calls / results appended
   as they land (polled), with a "▼ N new" pill when you've scrolled up.
3. A **live terminal tail** — the last lines of the actual Claude Code screen,
   the ground truth you can never be fooled by.

Transport is short-interval **polling** (not websockets): the page runs behind a
cloudflared/reverse-proxy tunnel where websockets are unreliable, and a 1.5s poll
is plenty for human-paced activity. The API is read-only; the live token is
HMAC-signed, purpose-scoped, and cannot mutate anything.
"""

from __future__ import annotations

import html
import json
import os
from typing import TYPE_CHECKING

import structlog

from ccgram.miniapp.server import _BOT_TOKEN_KEY  # type: ignore[attr-defined]

from ..output_pipeline.transcript_events import (
    TurnEvent,
    events_from_lines,
    extract_events,
)
from ..share.tokens import InvalidShareToken, verify_live_token
from ._page_shell import error_page, render_page
from .live_status import LiveStatus, derive_status, terminal_tail
from .transcript_render import render_rows_html, transcript_css

if TYPE_CHECKING:
    from aiohttp import web

logger = structlog.get_logger()

_POLL_MS = 1500  # client poll interval
_TERMINAL_LINES = 16  # ground-truth terminal tail height
_INITIAL_EVENTS_LINES = 60  # recent history shown on first paint


def _resolve_window(request: "web.Request") -> str:
    """Return the window id from the live token, or raise InvalidShareToken."""
    token = request.match_info.get("token", "")
    bot_token = request.app[_BOT_TOKEN_KEY]
    return verify_live_token(token, bot_token=bot_token).share_id


def _transcript_path(window_id: str) -> str | None:
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.window_query import view_window

    try:
        view = view_window(window_id)
    except RuntimeError:
        return None
    path = getattr(view, "transcript_path", None) if view else None
    return str(path) if path else None


def _file_size(path: str | None) -> int:
    if not path:
        return 0
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _read_new_events(path: str | None, offset: int) -> tuple[list[TurnEvent], int]:
    """Parse transcript events written past *offset*; return (events, new_offset).

    Only whole JSONL lines are consumed (a half-written trailing line is re-read
    next poll). Detects truncation/rotation and resets the offset.
    """
    if not path:
        return [], offset
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size < offset:
                offset = 0
            fh.seek(offset)
            chunk = fh.read()
    except OSError:
        return [], offset
    newline = chunk.rfind(b"\n")
    if newline == -1:
        return [], offset
    consumed = chunk[: newline + 1]
    lines = consumed.decode("utf-8", errors="replace").splitlines()
    return events_from_lines(lines), offset + len(consumed)


async def _capture_pane(window_id: str) -> str:
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.tmux_manager import tmux_manager

    try:
        text = await tmux_manager.capture_pane(window_id)
    except Exception:  # noqa: BLE001 -- capture must never break the live view
        return ""
    return text or ""


async def _snapshot(window_id: str) -> tuple[LiveStatus, list[str]]:
    """Current status + terminal tail from a fresh pane capture."""
    pane = await _capture_pane(window_id)
    if not pane:
        return LiveStatus("idle", "Session ended", "", overlay=False), []
    return derive_status(pane), terminal_tail(pane, _TERMINAL_LINES)


# ── Page ────────────────────────────────────────────────────────────────


async def _handle_live(request: "web.Request") -> "web.Response":
    # Lazy: aiohttp only needed inside the request handler.
    from aiohttp import web

    try:
        window_id = _resolve_window(request)
    except InvalidShareToken as exc:
        logger.debug("live rejected: %s", exc)
        return web.Response(
            status=403,
            text=error_page("This live link is invalid or expired."),
            content_type="text/html",
        )

    path = _transcript_path(window_id)
    initial_events = (
        extract_events(path, max_lines=_INITIAL_EVENTS_LINES) if path else []
    )
    initial_rows = render_rows_html(initial_events)
    status, terminal = await _snapshot(window_id)

    token = request.match_info.get("token", "")
    config = {
        "api": f"/api/live/{token}",
        "offset": _file_size(path),
        "pollMs": _POLL_MS,
        "status0": status.to_dict(),
        "terminal0": terminal,
    }
    body = _page_body(
        window_id=window_id, initial_rows=initial_rows, config_json=json.dumps(config)
    )
    page = render_page(
        title=f"Live · {window_id}",
        body_html=body,
        extra_css=transcript_css() + _LIVE_CSS,
    )
    return web.Response(text=page, content_type="text/html")


def _page_body(*, window_id: str, initial_rows: str, config_json: str) -> str:
    wid = html.escape(window_id)
    rows = initial_rows or '<p class="empty">Waiting for activity…</p>'
    return (
        '<div class="live-head">'
        '  <div id="banner" class="banner">'
        '    <span class="dot"></span>'
        '    <span id="banner-label" class="banner-label">Connecting…</span>'
        '    <span id="banner-detail" class="banner-detail"></span>'
        "  </div>"
        f'  <div class="live-meta">🪟 <code>{wid}</code> · live</div>'
        "</div>"
        '<details class="terminal-wrap" open>'
        "  <summary>🖥 Terminal (live)</summary>"
        '  <pre id="terminal" class="terminal"></pre>'
        "</details>"
        f'<div class="transcript" id="stream">{rows}</div>'
        '<button id="newpill" class="newpill" hidden>▼ <span id="newcount">0</span> new</button>'
        f'<script id="live-config" type="application/json">{config_json}</script>'
        f"<script>{_LIVE_JS}</script>"
    )


# ── Polling API ─────────────────────────────────────────────────────────


async def _handle_live_api(request: "web.Request") -> "web.Response":
    # Lazy: aiohttp only needed inside the request handler.
    from aiohttp import web

    try:
        window_id = _resolve_window(request)
    except InvalidShareToken:
        return web.json_response({"error": "invalid"}, status=403)

    try:
        offset = int(request.query.get("offset", "0"))
    except ValueError:
        offset = 0

    path = _transcript_path(window_id)
    events, new_offset = _read_new_events(path, offset)
    status, terminal = await _snapshot(window_id)

    return web.json_response(
        {
            "status": status.to_dict(),
            "terminal": terminal,
            "rows_html": render_rows_html(events),
            "num_new": len(events),
            "offset": new_offset,
        }
    )


def register_live_routes(app: "web.Application") -> None:
    app.router.add_get("/live/{token}", _handle_live)
    app.router.add_get("/api/live/{token}", _handle_live_api)


# ── Assets ──────────────────────────────────────────────────────────────

_LIVE_CSS = """
  .live-head { position: sticky; top: 0; z-index: 5; background: var(--bg);
      padding: 6px 0 12px; margin: -8px 0 8px; }
  .banner { display: flex; align-items: center; gap: 10px; padding: 11px 14px;
      border-radius: 12px; border: 1px solid var(--border);
      background: var(--surface); font-weight: 600; }
  .banner .dot { width: 10px; height: 10px; border-radius: 50%;
      background: var(--faint); flex: 0 0 auto; }
  .banner-detail { color: var(--muted); font-weight: 400; font-size: 0.85rem;
      margin-left: auto; text-align: right; }
  .banner.working { border-color: #2f7d4f; background: rgba(47,125,79,.14); }
  .banner.working .dot { background: #37d67a; animation: pulse 1.1s ease-in-out infinite; }
  .banner.awaiting { border-color: #b98b1f; background: rgba(185,139,31,.14); }
  .banner.awaiting .dot { background: #f5c451; }
  .banner.idle .dot { background: var(--faint); }
  .banner.overlay { border-color: #b5502f; background: rgba(181,80,47,.16); }
  .banner.overlay .dot { background: #ff8a5c; }
  @keyframes pulse { 0%,100% { opacity: 1; transform: scale(1); }
      50% { opacity: .35; transform: scale(1.5); } }
  .live-meta { color: var(--faint); font-size: 0.76rem; margin-top: 7px; padding-left: 3px; }
  .live-meta code { color: var(--accent); font-family: var(--mono); }

  .terminal-wrap { border: 1px solid var(--border); border-radius: 12px;
      background: #06070a; margin: 0 0 16px; }
  .terminal-wrap > summary { cursor: pointer; padding: 9px 13px; color: var(--muted);
      font-size: 0.8rem; user-select: none; list-style: none; }
  .terminal-wrap > summary::-webkit-details-marker { display: none; }
  .terminal { margin: 0; padding: 10px 13px 13px; font-family: var(--mono);
      font-size: 12px; line-height: 1.45; color: #cdd3df; white-space: pre-wrap;
      word-break: break-word; max-height: 320px; overflow: auto; }

  .newpill { position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%);
      z-index: 10; margin: 0; padding: 9px 16px; border: 0; border-radius: 999px;
      background: linear-gradient(140deg,#6d8bff,#b69cff); color: #0b0d12;
      font-weight: 650; font-size: 0.85rem; cursor: pointer; box-shadow: var(--shadow); }
"""

_LIVE_JS = """
(function () {
  var cfg = JSON.parse(document.getElementById('live-config').textContent);
  var offset = cfg.offset || 0;
  var pending = 0;
  var banner = document.getElementById('banner');
  var label = document.getElementById('banner-label');
  var detail = document.getElementById('banner-detail');
  var stream = document.getElementById('stream');
  var term = document.getElementById('terminal');
  var pill = document.getElementById('newpill');
  var newcount = document.getElementById('newcount');

  function nearBottom() {
    return (window.innerHeight + window.scrollY) >= (document.body.offsetHeight - 140);
  }
  function toBottom() { window.scrollTo(0, document.body.scrollHeight); }
  function setStatus(s) {
    s = s || {};
    banner.className = 'banner ' + (s.state || 'idle') + (s.overlay ? ' overlay' : '');
    label.textContent = s.label || 'Idle';
    detail.textContent = s.detail || '';
    var t = (s.label || 'Live') + ' · ' + (cfg.wid || '');
    if (document.title !== t) document.title = t;
  }
  function setTerminal(lines) {
    term.textContent = (lines && lines.length) ? lines.join('\\n') : '(no terminal output)';
  }
  function refreshPill() {
    if (pending > 0) { newcount.textContent = pending; pill.hidden = false; }
    else { pill.hidden = true; }
  }
  pill.addEventListener('click', function () {
    pending = 0; refreshPill(); toBottom();
  });

  function apply(d) {
    if (typeof d.offset === 'number') offset = d.offset;
    setStatus(d.status);
    setTerminal(d.terminal);
    if (d.rows_html) {
      var wasNear = nearBottom();
      stream.insertAdjacentHTML('beforeend', d.rows_html);
      if (wasNear) { pending = 0; toBottom(); }
      else { pending += (d.num_new || 0); }
      refreshPill();
    }
  }

  function poll() {
    fetch(cfg.api + '?offset=' + offset, { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) apply(d); })
      .catch(function () { /* transient — keep polling */ })
      .then(function () { setTimeout(poll, cfg.pollMs || 1500); });
  }

  setStatus(cfg.status0);
  setTerminal(cfg.terminal0);
  toBottom();
  poll();
})();
"""

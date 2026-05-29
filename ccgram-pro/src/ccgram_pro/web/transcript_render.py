"""Render structured turn events into a polished chat-transcript HTML page.

Design goals (the previous flat-markdown dump failed all of these):

- **Instantly scannable roles.** User and Claude messages sit on opposite
  sides with distinct avatars + colors, like a chat app.
- **Tool calls read as cards**, not inline code soup — a labelled header
  (🔧 Bash) with the command, and the result folded into a collapsible
  ``<details>`` directly beneath the call it belongs to.
- **Thinking is muted + collapsed** so it never competes with the answer.
- **Long output truncates** with a "show more" affordance (native
  ``<details>``) so a 500-line tool result doesn't bury everything after
  it.
- **Mobile-first, dark, zero JS, zero CDN** — native ``<details>`` gives
  collapse without scripting; first paint is instant behind any proxy.

XSS: every text field is ``html.escape``-d. The only markup we inject is
our own structural tags. A minimal markdown pass (fenced + inline code)
runs *after* escaping so Claude's code blocks render without ever
allowing live HTML.
"""

from __future__ import annotations

import html
import json
import re

from ..output_pipeline.transcript_events import TurnEvent

# How many characters of a tool result to show before folding the rest.
_RESULT_PREVIEW_CHARS = 1200
_TOOL_INPUT_PREVIEW_CHARS = 2000


def _md_inline(escaped: str) -> str:
    """Inline markdown on already-escaped text: `code`, **bold**."""
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


def _render_message_text(text: str) -> str:
    """Render assistant/user text: escape, then fenced + inline code, paragraphs."""
    escaped = html.escape(text)

    def _fenced(match: "re.Match[str]") -> str:
        return f"<pre><code>{match.group(1)}</code></pre>"

    escaped = re.sub(r"```[a-zA-Z0-9_-]*\n?([\s\S]*?)```", _fenced, escaped)
    # Split into paragraphs on blank lines; single newlines become <br>.
    paragraphs = [
        f"<p>{_md_inline(part).replace(chr(10), '<br>')}</p>"
        for part in re.split(r"\n\s*\n", escaped.strip())
        if part.strip()
    ]
    return "\n".join(paragraphs) or "<p><em>(empty)</em></p>"


def _format_tool_input(tool_input: dict | None) -> str:
    """Pick the most human-readable rendering of a tool's input."""
    if not tool_input:
        return ""
    # Common shapes: Bash → {command, description}; Edit → {file_path,
    # old_string, new_string}; Read → {file_path}. Show the headline field
    # prominently, the rest as JSON.
    headline_keys = ("command", "file_path", "path", "pattern", "query", "url")
    headline = ""
    for key in headline_keys:
        if key in tool_input and isinstance(tool_input[key], str):
            headline = tool_input[key]
            break
    body = json.dumps(tool_input, indent=2, sort_keys=True, ensure_ascii=False)
    if len(body) > _TOOL_INPUT_PREVIEW_CHARS:
        body = body[:_TOOL_INPUT_PREVIEW_CHARS] + "\n… (truncated)"
    if headline:
        return (
            f'<div class="tool-headline">{html.escape(headline)}</div>'
            f'<details class="tool-args"><summary>arguments</summary>'
            f"<pre>{html.escape(body)}</pre></details>"
        )
    return f"<pre>{html.escape(body)}</pre>"


def _tool_emoji(tool_name: str) -> str:
    name = tool_name.lower()
    return {
        "bash": "💻",
        "read": "📖",
        "edit": "✏️",
        "write": "📝",
        "grep": "🔍",
        "glob": "🗂️",
        "task": "🤖",
        "webfetch": "🌐",
        "websearch": "🔎",
        "todowrite": "✅",
    }.get(name, "🔧")


def _render_tool_use(ev: TurnEvent) -> str:
    emoji = _tool_emoji(ev.tool_name)
    return (
        '<div class="row tool">'
        '  <div class="gutter"><span class="avatar tool-avatar">🔧</span></div>'
        '  <div class="bubble tool-bubble">'
        f'    <div class="tool-head">{emoji} <strong>{html.escape(ev.tool_name)}</strong></div>'
        f"    {_format_tool_input(ev.tool_input)}"
        "  </div>"
        "</div>"
    )


def _render_tool_result(ev: TurnEvent) -> str:
    text = ev.text or "(no output)"
    truncated = len(text) > _RESULT_PREVIEW_CHARS
    preview = text[:_RESULT_PREVIEW_CHARS]
    cls = "tool-result error" if ev.is_error else "tool-result"
    label = "⚠️ error output" if ev.is_error else "output"
    if truncated:
        body = (
            f"<pre>{html.escape(preview)}</pre>"
            f'<details class="more"><summary>show {len(text) - _RESULT_PREVIEW_CHARS} more chars</summary>'
            f"<pre>{html.escape(text[_RESULT_PREVIEW_CHARS:])}</pre></details>"
        )
    else:
        body = f"<pre>{html.escape(text)}</pre>"
    return (
        f'<div class="row tool">'
        '  <div class="gutter"></div>'
        f'  <details class="bubble {cls}" open>'
        f"    <summary>{label}</summary>"
        f"    {body}"
        "  </details>"
        "</div>"
    )


def _render_thinking(ev: TurnEvent) -> str:
    return (
        '<div class="row assistant">'
        '  <div class="gutter"></div>'
        '  <details class="bubble thinking">'
        "    <summary>💭 Thinking</summary>"
        f'    <div class="content">{_render_message_text(ev.text)}</div>'
        "  </details>"
        "</div>"
    )


def _render_user(ev: TurnEvent) -> str:
    return (
        '<div class="row user">'
        '  <div class="bubble user-bubble">'
        '    <div class="role-label">You</div>'
        f'    <div class="content">{_render_message_text(ev.text)}</div>'
        "  </div>"
        '  <div class="gutter"><span class="avatar user-avatar">🧑</span></div>'
        "</div>"
    )


def _render_assistant(ev: TurnEvent) -> str:
    return (
        '<div class="row assistant">'
        '  <div class="gutter"><span class="avatar claude-avatar">✦</span></div>'
        '  <div class="bubble assistant-bubble">'
        '    <div class="role-label">Claude</div>'
        f'    <div class="content">{_render_message_text(ev.text)}</div>'
        "  </div>"
        "</div>"
    )


def render_rows_html(events: list[TurnEvent]) -> str:
    """Render just the event rows (no ``.transcript`` container).

    Used both for the initial page (inside the container) and the
    infinite-scroll fragment endpoint (prepended into the existing
    container client-side).
    """
    parts: list[str] = []
    for ev in events:
        if ev.kind == "user":
            parts.append(_render_user(ev))
        elif ev.kind == "assistant":
            parts.append(_render_assistant(ev))
        elif ev.kind == "thinking":
            parts.append(_render_thinking(ev))
        elif ev.kind == "tool_use":
            parts.append(_render_tool_use(ev))
        elif ev.kind == "tool_result":
            parts.append(_render_tool_result(ev))
    return "\n".join(parts)


def render_events_html(events: list[TurnEvent]) -> str:
    """Render the full event list into a ``.transcript`` container."""
    if not events:
        return '<p class="empty">No transcript content.</p>'
    return f'<div class="transcript">{render_rows_html(events)}</div>'


def transcript_css() -> str:
    """CSS for the transcript layout. Concatenated into the page <style>."""
    return """
  .transcript { display: flex; flex-direction: column; gap: 14px; }
  .row { display: flex; gap: 10px; align-items: flex-start; }
  .row.user { flex-direction: row; justify-content: flex-end; }
  .gutter { flex: 0 0 32px; display: flex; justify-content: center; padding-top: 2px; }
  .avatar { width: 30px; height: 30px; border-radius: 50%; display: flex;
            align-items: center; justify-content: center; font-size: 15px;
            border: 1px solid var(--border); }
  .claude-avatar { background: #1f6feb22; color: #58a6ff; }
  .user-avatar { background: #23863622; }
  .tool-avatar { background: #9e6a0322; }
  .bubble { border-radius: 12px; padding: 10px 14px; max-width: 82%;
            border: 1px solid var(--border); background: var(--code-bg); }
  .role-label { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em;
                color: var(--muted); margin-bottom: 4px; font-weight: 600; }
  .user-bubble { background: #1f6feb1a; border-color: #1f6feb55; }
  .assistant-bubble { background: #161b22; }
  .content p { margin: 0 0 0.6em; }
  .content p:last-child { margin-bottom: 0; }
  .content pre { background: #0d1117; border: 1px solid var(--border);
                 border-radius: 8px; padding: 10px 12px; overflow-x: auto; margin: 0.5em 0; }
  .content code { background: #0d1117; padding: 1px 5px; border-radius: 4px;
                  font-family: "SF Mono", ui-monospace, Menlo, monospace; font-size: 0.9em; }
  .content pre code { background: none; padding: 0; }

  /* Tool call card */
  .tool-bubble { background: #1c1810; border-color: #9e6a0355; max-width: 90%; }
  .tool-head { font-size: 0.95rem; margin-bottom: 6px; }
  .tool-headline { font-family: "SF Mono", ui-monospace, Menlo, monospace;
                   font-size: 0.86rem; background: #0d1117; border: 1px solid var(--border);
                   border-radius: 6px; padding: 8px 10px; overflow-x: auto;
                   white-space: pre-wrap; word-break: break-all; }
  .tool-args { margin-top: 6px; }
  .tool-args summary, .tool-result summary, .thinking summary, .more summary {
      cursor: pointer; color: var(--muted); font-size: 0.8rem; user-select: none; }
  .tool-args pre, .tool-result pre, .more pre { font-size: 0.82rem; margin: 6px 0 0;
      background: #0d1117; border: 1px solid var(--border); border-radius: 6px;
      padding: 8px 10px; overflow-x: auto; white-space: pre-wrap; word-break: break-word; }

  /* Tool result */
  .tool-result { background: #0f1a12; border-color: #2ea04355; max-width: 90%; }
  .tool-result.error { background: #1a1011; border-color: #f8514955; }
  .tool-result[open] summary { margin-bottom: 6px; }

  /* Thinking */
  .thinking { background: #14141c; border-color: #6e768166; max-width: 90%; opacity: 0.85; }
  .thinking .content { margin-top: 6px; font-size: 0.9rem; }

  .empty { color: var(--muted); }
  @media (max-width: 600px) {
    .bubble, .tool-bubble, .tool-result, .thinking { max-width: 100%; }
  }
"""

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
    """CSS for the transcript layout. Concatenated into the page <style>.

    Modern dark UI: gradient avatars, soft elevated bubbles with depth,
    refined type, and styled native-<details> disclosure chevrons. Uses
    the :root tokens defined in the page shell (--surface, --accent, …).
    """
    return """
  .transcript { display: flex; flex-direction: column; gap: 16px; }
  .row { display: flex; gap: 11px; align-items: flex-start; }
  .row.user { flex-direction: row; justify-content: flex-end; }

  .avatar { flex: 0 0 32px; width: 32px; height: 32px; border-radius: 11px;
            display: flex; align-items: center; justify-content: center;
            font-size: 15px; box-shadow: var(--shadow); }
  .claude-avatar { background: linear-gradient(140deg, #6d8bff, #b69cff);
                   color: #0b0d12; font-weight: 700; }
  .user-avatar { background: linear-gradient(140deg, #2b3242, #363d4e); }
  .tool-avatar { background: linear-gradient(140deg, #3a2f12, #4a3a14); }
  .gutter { flex: 0 0 32px; }

  .bubble { border-radius: var(--radius); padding: 11px 15px; max-width: 80%;
            border: 1px solid var(--border-soft); background: var(--surface);
            box-shadow: var(--shadow); }
  .role-label { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.06em;
                color: var(--faint); margin-bottom: 5px; font-weight: 600; }
  .user-bubble { background: linear-gradient(160deg, #1d2740, #151a26);
                 border-color: #2c3a63; border-bottom-right-radius: 6px; }
  .assistant-bubble { border-bottom-left-radius: 6px; }
  .content p { margin: 0 0 0.62em; }
  .content p:last-child { margin-bottom: 0; }
  .content strong { color: #fff; font-weight: 640; }
  .content pre { background: #0b0e14; border: 1px solid var(--border-soft);
                 border-radius: 10px; padding: 11px 13px; overflow-x: auto;
                 margin: 0.55em 0; }
  .content code { background: var(--elevated); padding: 1.5px 6px; border-radius: 6px;
                  font-family: var(--mono); font-size: 0.88em; }
  .content pre code { background: none; padding: 0; }

  /* Tool call card — amber accent */
  .tool-bubble { background: linear-gradient(160deg, #1c1709, #161208);
                 border-color: #4a3a14; border-left: 3px solid #d8a23a;
                 max-width: 92%; }
  .tool-head { font-size: 0.92rem; font-weight: 600; margin-bottom: 7px;
               color: #f0c674; }
  .tool-headline { font-family: var(--mono); font-size: 0.84rem; background: #0b0e14;
                   border: 1px solid var(--border-soft); border-radius: 8px;
                   padding: 9px 11px; overflow-x: auto; white-space: pre-wrap;
                   word-break: break-word; color: #e6d9b8; }
  .tool-args { margin-top: 7px; }
  details > summary {
      cursor: pointer; color: var(--muted); font-size: 0.78rem; font-weight: 550;
      user-select: none; list-style: none; display: inline-flex; align-items: center;
      gap: 5px; padding: 1px 0; }
  details > summary::-webkit-details-marker { display: none; }
  details > summary::before { content: '▸'; color: var(--faint); font-size: 0.7rem;
      transition: transform .15s ease; display: inline-block; }
  details[open] > summary::before { transform: rotate(90deg); }
  .tool-args pre, .tool-result pre, .more pre { font-size: 0.81rem; margin: 7px 0 0;
      background: #0b0e14; border: 1px solid var(--border-soft); border-radius: 8px;
      padding: 9px 11px; overflow-x: auto; white-space: pre-wrap; word-break: break-word; }

  /* Tool result — emerald (success) / rose (error) */
  .tool-result { background: linear-gradient(160deg, #0c1810, #0a130c);
                 border-color: #1c3a28; border-left: 3px solid #2ea043; max-width: 92%; }
  .tool-result.error { background: linear-gradient(160deg, #1a0e10, #140a0b);
                       border-color: #3a1c1f; border-left-color: #f85149; }
  .tool-result[open] > summary { margin-bottom: 7px; }

  /* Thinking — muted violet, recessed */
  .thinking { background: #13131d; border-color: #2a2740; border-left: 3px solid #6e6a9e;
              max-width: 92%; }
  .thinking .content { margin-top: 7px; font-size: 0.9rem; color: var(--muted); }

  .empty { color: var(--muted); text-align: center; padding: 24px; }
  @media (max-width: 600px) {
    .bubble, .tool-bubble, .tool-result, .thinking { max-width: 100%; }
  }
"""

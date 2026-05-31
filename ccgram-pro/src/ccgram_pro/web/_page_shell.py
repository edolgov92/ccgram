"""Shared dark page shell for layer web surfaces (composer + error pages).

A single escaping-safe HTML wrapper so new surfaces match the diff/transcript
theme without copy-pasting the CSS a third time. Callers pass already-escaped
*body_html*; the shell never interpolates untrusted text outside that slot
except *title*, which it escapes itself.
"""

from __future__ import annotations

import html

_CSS = """
  :root { color-scheme: dark; --bg:#0a0c10; --surface:#12151d; --elevated:#171b25;
    --fg:#eceef4; --muted:#99a1b3; --faint:#6b7280; --accent:#8aa6ff;
    --border:#232936; --border-soft:#1b212c; --danger:#ff8a8a;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.22);
    --font: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
    --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font-family:var(--font);
    font-size:15.5px; line-height:1.6; -webkit-font-smoothing:antialiased; }
  main { max-width:720px; margin:0 auto; padding:24px 16px 96px; }
  h1 { font-size:1.2rem; font-weight:650; letter-spacing:-0.01em; margin:0 0 16px; }
  .chip { display:inline-block; font-size:0.72rem; color:var(--muted); background:var(--surface);
    border:1px solid var(--border-soft); border-radius:999px; padding:3px 10px; margin:0 4px 8px 0; }
  .chip code { color:var(--accent); font-family:var(--mono); }
  label { display:block; margin:14px 0 6px; font-weight:550; color:var(--muted); font-size:0.85rem; }
  input[type=text], textarea, select { width:100%; background:var(--elevated); color:var(--fg);
    border:1px solid var(--border); border-radius:10px; padding:10px 12px; font-family:var(--font);
    font-size:1rem; }
  textarea { resize:vertical; min-height:140px; font-family:var(--mono); font-size:0.92rem; }
  .row { display:flex; gap:10px; align-items:center; margin-top:12px; }
  button { margin-top:20px; background:linear-gradient(140deg,#6d8bff,#b69cff); color:#0b0d12;
    border:0; border-radius:10px; padding:12px 20px; font-weight:650; font-size:1rem;
    cursor:pointer; box-shadow:var(--shadow); }
  a { color:var(--accent); }
  .err { color:var(--danger); }
  footer { margin-top:44px; padding-top:18px; border-top:1px solid var(--border-soft);
    color:var(--faint); font-size:0.76rem; text-align:center; }
"""


def render_page(
    *, title: str, body_html: str, footer: str = "ccgram-pro", extra_css: str = ""
) -> str:
    """Return a full dark HTML page. *body_html* must already be escaped.

    *extra_css* is appended verbatim to the page ``<style>`` so callers can pull
    in component CSS (e.g. ``transcript_css()``) without a second ``<style>``.
    """
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<meta name="color-scheme" content="dark light">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_CSS}{extra_css}</style>\n</head>\n<body>\n<main>\n"
        f"{body_html}\n"
        f"<footer>{html.escape(footer)}</footer>\n"
        "</main>\n</body>\n</html>\n"
    )


def error_page(message: str, *, title: str = "Unavailable") -> str:
    """Render a styled error body (message is escaped)."""
    return render_page(
        title=title,
        body_html=f'<h1>{html.escape(title)}</h1><p class="err">{html.escape(message)}</p>',
    )

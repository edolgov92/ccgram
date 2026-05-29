"""Render parsed unified diffs to HTML. Pure stdlib + html.escape."""

from __future__ import annotations

import html

from ..git_ops.diff import DiffFile

_DIFF_CSS = """\
  /* Diff colours match GitHub-dark for familiarity. */
  .diff { font-family: "SF Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
          font-size: 0.85rem; line-height: 1.5; }
  .file { border: 1px solid var(--border); border-radius: 8px; margin-bottom: 18px;
          overflow: hidden; }
  .file > header { background: var(--code-bg); padding: 8px 12px; border-bottom: 1px solid var(--border);
                   font-weight: 600; word-break: break-all; }
  .file.binary > .body { padding: 12px; color: var(--muted); font-style: italic; }
  .hunk-header { background: #1f2937; color: var(--muted); padding: 4px 10px;
                 border-top: 1px solid var(--border); }
  .line { display: flex; gap: 6px; padding: 1px 10px; white-space: pre-wrap;
          word-break: break-all; }
  .line .gut { color: var(--muted); user-select: none; min-width: 1.2em; text-align: right; }
  .line .body { flex: 1; }
  .line.add { background: rgba(46, 160, 67, 0.15); }
  .line.add .gut { color: #3fb950; }
  .line.del { background: rgba(248, 81, 73, 0.15); }
  .line.del .gut { color: #f85149; }
  .file-list { background: var(--code-bg); border: 1px solid var(--border);
               border-radius: 8px; padding: 10px 14px; margin-bottom: 16px; }
  .file-list a { color: var(--accent); text-decoration: none; }
  .file-list a:hover { text-decoration: underline; }
  .stats { color: var(--muted); font-size: 0.8rem; margin-left: 8px; }
"""


def _stat_for(file: DiffFile) -> tuple[int, int]:
    adds = sum(1 for hunk in file.hunks for marker, _ in hunk.lines if marker == "+")
    dels = sum(1 for hunk in file.hunks for marker, _ in hunk.lines if marker == "-")
    return adds, dels


def render_diff_html(
    files: list[DiffFile], *, empty_message: str = "No changes."
) -> str:
    """Return the HTML body for a list of parsed diff files."""
    if not files:
        return f'<p style="color: var(--muted);">{html.escape(empty_message)}</p>'
    parts: list[str] = [_render_file_list(files)]
    for idx, file in enumerate(files):
        parts.append(_render_file(file, idx))
    return "\n".join(parts)


def _render_file_list(files: list[DiffFile]) -> str:
    rows: list[str] = ['<div class="file-list"><strong>Files changed:</strong><br>']
    for idx, file in enumerate(files):
        adds, dels = _stat_for(file)
        path = html.escape(file.path or file.old_path or "(no path)")
        rows.append(
            f'<a href="#f{idx}">{path}</a>'
            f'<span class="stats">+{adds} −{dels}</span><br>'
        )
    rows.append("</div>")
    return "".join(rows)


def _render_file(file: DiffFile, idx: int) -> str:
    rename_note = ""
    if file.old_path and file.path and file.old_path != file.path:
        rename_note = (
            f' <span class="stats">renamed from {html.escape(file.old_path)}</span>'
        )
    header_title = html.escape(file.path or file.old_path or "")
    parts: list[str] = [
        f'<section class="file{"" if not file.binary else " binary"}" id="f{idx}">',
        f"  <header>{header_title}{rename_note}</header>",
    ]
    if file.binary:
        parts.append('  <div class="body">Binary file — diff not shown.</div>')
        parts.append("</section>")
        return "\n".join(parts)
    if not file.hunks:
        parts.append(
            '  <div class="body" style="padding: 12px; color: var(--muted);">No hunks recorded (rename / mode-only?).</div>'
        )
        parts.append("</section>")
        return "\n".join(parts)
    parts.append('  <div class="body">')
    for hunk in file.hunks:
        parts.append(f'    <div class="hunk-header">{html.escape(hunk.header)}</div>')
        old_no = hunk.old_start
        new_no = hunk.new_start
        for marker, content in hunk.lines:
            row_class = "add" if marker == "+" else ("del" if marker == "-" else "")
            if marker == "+":
                gutter = f"+{new_no}"
                new_no += 1
            elif marker == "-":
                gutter = f"−{old_no}"
                old_no += 1
            else:
                gutter = f"{new_no}"
                old_no += 1
                new_no += 1
            parts.append(
                f'    <div class="line {row_class}">'
                f'<span class="gut">{gutter}</span>'
                f'<span class="body">{html.escape(content)}</span>'
                "</div>"
            )
    parts.append("  </div>")
    parts.append("</section>")
    return "\n".join(parts)


def diff_page_css() -> str:
    """CSS block for the diff page; concat into the page template's <style>."""
    return _DIFF_CSS

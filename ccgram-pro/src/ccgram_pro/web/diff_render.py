"""Render parsed unified diffs to a production-grade HTML diff view.

Features over the old flat renderer:

- A line-number gutter (old | new) per row.
- Strong add/removed highlighting plus **word-level** intra-line ``<mark>``
  highlighting on paired changed lines (``difflib`` on whitespace tokens).
- Per-file collapse via native ``<details>``.
- **Context expansion**: ``▲/▼`` expanders between/around hunks fetch more
  unchanged lines on demand from ``/diff/{token}/expand`` (see ``diff_js``).

Pure stdlib + ``html.escape``; the only client JS is the small expander
fetcher. Payload caps bound files / lines / line length, but expansion lets the
user pull more on demand instead of hitting a silent wall.
"""

from __future__ import annotations

import difflib
import html
import re

from ..git_ops.diff import DiffFile, DiffHunk

_MAX_FILES = 120
_MAX_LINES_PER_FILE = 2000
_MAX_LINE_LEN = 2000
_WORD_DIFF_MAX_LEN = 600  # skip word-diff on very long lines (perf)
_WORD_RE = re.compile(r"\s+|\S+")


def _stat_for(file: DiffFile) -> tuple[int, int]:
    adds = sum(1 for h in file.hunks for marker, _ in h.lines if marker == "+")
    dels = sum(1 for h in file.hunks for marker, _ in h.lines if marker == "-")
    return adds, dels


def _word_diff(old: str, new: str) -> tuple[str, str]:
    """Return (old_html, new_html) with changed word spans wrapped in ``<mark>``."""
    if len(old) > _WORD_DIFF_MAX_LEN or len(new) > _WORD_DIFF_MAX_LEN:
        return html.escape(old), html.escape(new)
    o = _WORD_RE.findall(old)
    n = _WORD_RE.findall(new)
    matcher = difflib.SequenceMatcher(a=o, b=n, autojunk=False)
    o_out: list[str] = []
    n_out: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        o_seg = html.escape("".join(o[i1:i2]))
        n_seg = html.escape("".join(n[j1:j2]))
        if tag == "equal":
            o_out.append(o_seg)
            n_out.append(n_seg)
        else:
            if o_seg:
                o_out.append(f"<mark>{o_seg}</mark>")
            if n_seg:
                n_out.append(f"<mark>{n_seg}</mark>")
    return "".join(o_out), "".join(n_out)


def _clip(content: str) -> str:
    return content if len(content) <= _MAX_LINE_LEN else content[:_MAX_LINE_LEN] + " …"


def _ctx_row(old_no: int, new_no: int, content: str) -> str:
    return (
        f'<tr class="ctx"><td class="ln">{old_no}</td><td class="ln">{new_no}</td>'
        f'<td class="code">{html.escape(_clip(content))}</td></tr>'
    )


def _del_row(old_no: int, html_body: str) -> str:
    return (
        f'<tr class="del"><td class="ln">{old_no}</td><td class="ln"></td>'
        f'<td class="code">{html_body}</td></tr>'
    )


def _add_row(new_no: int, html_body: str) -> str:
    return (
        f'<tr class="add"><td class="ln"></td><td class="ln">{new_no}</td>'
        f'<td class="code">{html_body}</td></tr>'
    )


def _expander(*, old_start: int, new_start: int, count: int | None) -> str:
    """A row whose button fetches more unchanged context (see ``diff_js``)."""
    count_attr = "" if count is None else str(count)
    label = "⤢ expand" if count is None else f"⤢ expand {count} lines"
    return (
        f'<tr class="expander" data-old="{old_start}" data-new="{new_start}" '
        f'data-count="{count_attr}"><td colspan="3">'
        f'<button class="exp" type="button">{label}</button></td></tr>'
    )


def _render_hunk_rows(hunk: DiffHunk, budget: list[int]) -> list[str]:
    """Render one hunk's lines (context/add/del + word-diff), respecting budget."""
    rows: list[str] = []
    old_no = hunk.old_start
    new_no = hunk.new_start
    lines = hunk.lines
    i = 0
    while i < len(lines):
        if budget[0] <= 0:
            break
        marker, content = lines[i]
        if marker == " ":
            rows.append(_ctx_row(old_no, new_no, content))
            old_no += 1
            new_no += 1
            budget[0] -= 1
            i += 1
            continue
        if marker == "+":
            rows.append(_add_row(new_no, html.escape(_clip(content))))
            new_no += 1
            budget[0] -= 1
            i += 1
            continue
        # A run of dels, then the following run of adds — pair for word-diff.
        dels: list[str] = []
        while i < len(lines) and lines[i][0] == "-":
            dels.append(lines[i][1])
            i += 1
        adds: list[str] = []
        while i < len(lines) and lines[i][0] == "+":
            adds.append(lines[i][1])
            i += 1
        for k, dtext in enumerate(dels):
            body = (
                _word_diff(dtext, adds[k])[0]
                if k < len(adds)
                else html.escape(_clip(dtext))
            )
            rows.append(_del_row(old_no, body))
            old_no += 1
            budget[0] -= 1
        for k, atext in enumerate(adds):
            body = (
                _word_diff(dels[k], atext)[1]
                if k < len(dels)
                else html.escape(_clip(atext))
            )
            rows.append(_add_row(new_no, body))
            new_no += 1
            budget[0] -= 1
    return rows


def _render_file(file: DiffFile, idx: int) -> str:
    adds, dels = _stat_for(file)
    path = html.escape(file.path or file.old_path or "(no path)")
    rename = ""
    if file.old_path and file.path and file.old_path != file.path:
        rename = f' <span class="stat">← {html.escape(file.old_path)}</span>'
    summary = (
        f"<summary>{path}"
        f'<span class="stat add">+{adds}</span>'
        f'<span class="stat del">−{dels}</span>{rename}</summary>'
    )
    if file.binary:
        return f'<details class="file" open>{summary}<div class="note">Binary file — diff not shown.</div></details>'
    if not file.hunks:
        return f'<details class="file" open>{summary}<div class="note">No textual changes (rename / mode-only).</div></details>'

    table_open = f'<table class="diff-body" data-path="{html.escape(file.path or file.old_path or "")}">'
    rows: list[str] = []
    budget = [_MAX_LINES_PER_FILE]
    prev_old_end = 1
    prev_new_end = 1
    for hi, hunk in enumerate(file.hunks):
        gap = hunk.new_start - prev_new_end
        if hi == 0 and hunk.new_start > 1:
            rows.append(_expander(old_start=1, new_start=1, count=hunk.new_start - 1))
        elif gap > 0:
            rows.append(
                _expander(old_start=prev_old_end, new_start=prev_new_end, count=gap)
            )
        rows.extend(_render_hunk_rows(hunk, budget))
        prev_old_end = hunk.old_start + hunk.old_count
        prev_new_end = hunk.new_start + hunk.new_count
        if budget[0] <= 0:
            rows.append(
                '<tr class="truncated"><td colspan="3">… file diff truncated; '
                "open the repo locally for the rest.</td></tr>"
            )
            break
    else:
        # Trailing expander (unknown length — JS loads in chunks until EOF).
        rows.append(
            _expander(old_start=prev_old_end, new_start=prev_new_end, count=None)
        )
    return (
        f'<details class="file" id="f{idx}" open>{summary}'
        f"{table_open}{''.join(rows)}</table></details>"
    )


def render_diff_files(files: list[DiffFile], *, empty_message: str) -> str:
    """Return the diff body HTML (file summary list + per-file tables)."""
    if not files:
        return f'<div class="empty">{html.escape(empty_message)}</div>'
    shown = files[:_MAX_FILES]
    parts: list[str] = [_render_file_list(files)]
    if len(files) > _MAX_FILES:
        parts.append(
            f'<div class="note">… {len(files) - _MAX_FILES} more file(s) omitted.</div>'
        )
    for idx, file in enumerate(shown):
        parts.append(_render_file(file, idx))
    return "\n".join(parts)


def _render_file_list(files: list[DiffFile]) -> str:
    rows = ['<div class="file-list"><div class="fl-head">Files changed</div>']
    for idx, file in enumerate(files[:_MAX_FILES]):
        adds, dels = _stat_for(file)
        path = html.escape(file.path or file.old_path or "(no path)")
        rows.append(
            f'<a href="#f{idx}"><span class="fl-path">{path}</span>'
            f'<span class="stat add">+{adds}</span>'
            f'<span class="stat del">−{dels}</span></a>'
        )
    rows.append("</div>")
    return "".join(rows)


def diff_css() -> str:
    return """
  .file-list { background: var(--surface); border: 1px solid var(--border-soft);
               border-radius: 12px; padding: 8px; margin-bottom: 18px; }
  .file-list .fl-head { font-size: 0.72rem; text-transform: uppercase;
               letter-spacing: 0.06em; color: var(--faint); padding: 4px 8px 8px; }
  .file-list a { display: flex; align-items: center; gap: 8px; text-decoration: none;
               color: var(--fg); padding: 6px 8px; border-radius: 8px; }
  .file-list a:hover { background: var(--elevated); }
  .file-list .fl-path { flex: 1; font-family: var(--mono); font-size: 0.82rem;
               word-break: break-all; }
  .stat { font-size: 0.74rem; font-family: var(--mono); }
  .stat.add { color: #3fb950; } .stat.del { color: #f85149; }
  details.file { border: 1px solid var(--border); border-radius: 10px;
               margin-bottom: 16px; overflow: hidden; background: var(--surface); }
  details.file > summary { cursor: pointer; padding: 10px 13px; font-family: var(--mono);
               font-size: 0.82rem; word-break: break-all; display: flex; gap: 9px;
               align-items: center; list-style: none; }
  details.file > summary::-webkit-details-marker { display: none; }
  details.file > summary::before { content: '▸'; color: var(--faint);
               transition: transform .15s ease; }
  details.file[open] > summary::before { transform: rotate(90deg); }
  details.file > summary .stat { margin-left: 0; }
  .note { padding: 12px 14px; color: var(--muted); font-style: italic;
          border-top: 1px solid var(--border-soft); }
  table.diff-body { width: 100%; border-collapse: collapse; border-top: 1px solid var(--border-soft);
          font-family: var(--mono); font-size: 0.82rem; line-height: 1.5; }
  table.diff-body td.ln { width: 1%; min-width: 42px; text-align: right; padding: 0 8px;
          color: var(--faint); user-select: none; vertical-align: top;
          border-right: 1px solid var(--border-soft); white-space: nowrap; }
  table.diff-body td.code { padding: 0 12px; white-space: pre-wrap; word-break: break-word; }
  tr.add { background: rgb(16 166 44 / 31%); } tr.add td.ln { color: #3fb950; }
  tr.del { background: rgba(248,81,73,.16); } tr.del td.ln { color: #f85149; }
  tr.add td.code mark { background: rgba(46,160,67,.40); color: #d7ffe0;
          border-radius: 3px; padding: 0 1px; }
  tr.del td.code mark { background: rgba(248,81,73,.40); color: #ffe0e0;
          border-radius: 3px; padding: 0 1px; }
  tr.expander td { padding: 0; }
  tr.expander button.exp { width: 100%; text-align: left; background: var(--elevated);
          color: var(--muted); border: 0; border-top: 1px solid var(--border-soft);
          border-bottom: 1px solid var(--border-soft); padding: 4px 14px; cursor: pointer;
          font-family: var(--mono); font-size: 0.76rem; }
  tr.expander button.exp:hover { color: var(--accent); }
  tr.truncated td { padding: 8px 14px; color: var(--muted); font-style: italic; }
  .empty { color: var(--muted); text-align: center; padding: 32px 16px; font-size: 1rem; }
"""


def diff_js() -> str:
    """Inline expander script. Expects ``DIFF_TOKEN`` + ``DIFF_ANCHOR`` globals."""
    return """
  function ccgExpand(btn){
    const row = btn.closest('tr.expander');
    const table = btn.closest('table.diff-body');
    if(!row || !table) return;
    const path = table.dataset.path;
    let oldNo = parseInt(row.dataset.old, 10);
    let newNo = parseInt(row.dataset.new, 10);
    const raw = row.dataset.count;
    const trailing = raw === '';
    const want = trailing ? 40 : parseInt(raw, 10);
    btn.disabled = true;
    fetch(`/diff/${DIFF_TOKEN}/expand?anchor=${DIFF_ANCHOR}`
          + `&path=${encodeURIComponent(path)}&start=${newNo}&count=${want}`)
      .then(r => r.ok ? r.json() : Promise.reject())
      .then(d => {
        const lines = d.lines || [];
        const frag = document.createDocumentFragment();
        for(const line of lines){
          const tr = document.createElement('tr'); tr.className = 'ctx';
          const o = document.createElement('td'); o.className = 'ln'; o.textContent = oldNo;
          const n = document.createElement('td'); n.className = 'ln'; n.textContent = newNo;
          const c = document.createElement('td'); c.className = 'code'; c.textContent = line;
          tr.appendChild(o); tr.appendChild(n); tr.appendChild(c);
          frag.appendChild(tr); oldNo++; newNo++;
        }
        row.parentNode.insertBefore(frag, row);
        if(trailing && lines.length >= want){
          row.dataset.old = oldNo; row.dataset.new = newNo; btn.disabled = false;
        } else {
          row.remove();
        }
      })
      .catch(() => { btn.textContent = '(expand failed)'; btn.disabled = false; });
  }
  document.addEventListener('click', e => {
    if(e.target && e.target.classList && e.target.classList.contains('exp')) ccgExpand(e.target);
  });
"""

"""Diff capture + unified-diff parsing.

``capture_diff_vs_ref`` runs ``git diff --unified=3 <ref>`` against the
working tree and returns the raw unified diff text.

``parse_unified_diff`` turns that text into a typed structure the web
viewer can render without owning its own parser. The parser is
deliberately minimal — supports the standard ``diff --git`` /
``--- a/<path>`` / ``+++ b/<path>`` / ``@@`` headers; binary diffs and
mode-only changes are recorded as files with no hunks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ._run import GitOpError, run_git


@dataclass(frozen=True)
class DiffHunk:
    """One ``@@ -X,Y +A,B @@`` block within a file."""

    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[str, str]] = field(default_factory=list)
    # Each entry is (marker, content) where marker ∈ {" ", "+", "-"}


@dataclass(frozen=True)
class DiffFile:
    """A single file's diff — header info + the parsed hunks."""

    path: str
    old_path: str | None
    binary: bool
    hunks: list[DiffHunk] = field(default_factory=list)


_HUNK_HEADER_RE = re.compile(
    r"^@@\s+-(?P<old_start>\d+)(?:,(?P<old_count>\d+))?\s+"
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))?\s+@@(?P<rest>.*)$"
)


def capture_diff_vs_ref(repo: Path | str, ref: str) -> str:
    """Return the unified diff between *ref* and the working tree.

    ``ref`` accepts anything ``git diff`` does — a sha, a branch, or
    ``HEAD~3``. ``--unified=3`` matches the convention of GitHub's PR
    view so the rendered output is familiar.
    """
    try:
        result = run_git(repo, "diff", "--unified=3", "--no-color", ref)
    except GitOpError as exc:
        # An empty diff isn't an error — git exits 0 with empty stdout.
        # Anything else (bad ref, etc.) we re-raise.
        raise exc
    return result.stdout


def parse_unified_diff(raw: str) -> list[DiffFile]:
    """Parse unified diff text into a list of :class:`DiffFile`."""
    files: list[DiffFile] = []
    current_file: dict[str, object] | None = None
    current_hunk: DiffHunk | None = None

    def flush_file() -> None:
        nonlocal current_file, current_hunk
        if current_file is None:
            return
        if current_hunk is not None:
            current_file.setdefault("hunks", []).append(current_hunk)  # type: ignore[union-attr]
        files.append(
            DiffFile(
                path=str(current_file.get("path", "")),
                old_path=current_file.get("old_path"),  # type: ignore[arg-type]
                binary=bool(current_file.get("binary", False)),
                hunks=list(current_file.get("hunks", [])),  # type: ignore[arg-type]
            )
        )
        current_file = None
        current_hunk = None

    for line in raw.splitlines():
        if line.startswith("diff --git "):
            flush_file()
            current_file = {"path": "", "old_path": None, "binary": False, "hunks": []}
            parts = line.split()
            if len(parts) >= 4:
                # diff --git a/<path> b/<path>
                current_file["old_path"] = parts[2][2:] if parts[2].startswith("a/") else parts[2]
                current_file["path"] = parts[3][2:] if parts[3].startswith("b/") else parts[3]
            continue
        if current_file is None:
            continue
        if line.startswith("Binary files"):
            current_file["binary"] = True
            continue
        if line.startswith("--- "):
            old = line[4:].strip()
            if old.startswith("a/"):
                old = old[2:]
            if old != "/dev/null":
                current_file["old_path"] = old
            continue
        if line.startswith("+++ "):
            new = line[4:].strip()
            if new.startswith("b/"):
                new = new[2:]
            if new != "/dev/null":
                current_file["path"] = new
            continue
        m = _HUNK_HEADER_RE.match(line)
        if m:
            if current_hunk is not None:
                current_file.setdefault("hunks", []).append(current_hunk)  # type: ignore[union-attr]
            current_hunk = DiffHunk(
                header=line,
                old_start=int(m.group("old_start")),
                old_count=int(m.group("old_count") or 1),
                new_start=int(m.group("new_start")),
                new_count=int(m.group("new_count") or 1),
                lines=[],
            )
            continue
        if current_hunk is None:
            continue
        if line.startswith(("+", "-", " ")):
            marker = line[0]
            content = line[1:]
            current_hunk.lines.append((marker, content))

    flush_file()
    return files

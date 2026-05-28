"""``.env`` reader/writer that preserves unknown keys and comments.

ccgram's runtime loads ``~/.ccgram/.env`` plus a local ``./.env`` via
``python-dotenv``, so the wizard's job is to update the keys it knows about
while leaving every other line (comments, blank lines, third-party keys)
untouched. The implementation is intentionally minimal — we own line
ordering for keys we set, append new keys to the end, and keep everything
else byte-identical.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import tempfile
from pathlib import Path

# Matches ``KEY=VALUE`` with whitespace tolerance. Values can be quoted or
# bare; the wizard only writes bare/double-quoted values, but parses any
# variant.
_KEY_RE = re.compile(r"^\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*?)\s*$")


_MIN_QUOTED_LEN = 2  # a paired quote on each end


def _strip_quotes(value: str) -> str:
    if (
        len(value) >= _MIN_QUOTED_LEN
        and value[0] == value[-1]
        and value[0] in ('"', "'")
    ):
        return value[1:-1]
    return value


def read_env(path: Path) -> dict[str, str]:
    """Return ``{KEY: VALUE}`` for every assignment in *path*.

    Comments, blank lines, and malformed lines are ignored. Values lose
    their surrounding quotes (matching python-dotenv's runtime behaviour).
    Missing file returns an empty dict.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _KEY_RE.match(raw)
        if not m:
            continue
        out[m.group("key")] = _strip_quotes(m.group("value"))
    return out


def _format_value(value: str) -> str:
    """Render *value* for the .env file.

    Bare assignment when the value is shell-safe (alphanumeric, dash,
    underscore, slash, dot, colon, comma); double-quoted otherwise, with
    embedded double-quotes and backslashes escaped. Matches python-dotenv's
    parse rules so the runtime sees the same string we wrote.
    """
    if value == "":
        return ""
    if re.fullmatch(r"[A-Za-z0-9_./:,@-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def update_env(path: Path, updates: dict[str, str]) -> None:
    """Merge *updates* into *path*, preserving unknown keys + comments.

    Existing assignments for the keys in *updates* are rewritten in place
    (preserving the surrounding lines); keys not yet present are appended
    to the end under a single ``# ccgram-pro setup`` block.

    Empty-string values are written as ``KEY=`` (no quotes) — this matches
    the convention python-dotenv uses for "set but blank".
    """
    if not updates:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()

    remaining = dict(updates)
    new_lines: list[str] = []
    for raw in existing_lines:
        m = _KEY_RE.match(raw)
        if m and m.group("key") in remaining:
            key = m.group("key")
            new_lines.append(f"{key}={_format_value(remaining.pop(key))}")
        else:
            new_lines.append(raw)

    if remaining:
        # Trailing-newline normalisation: ensure exactly one blank line
        # separator before our appended block, regardless of how the file
        # ended before.
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append("# Added by ccgram-pro setup")
        for key, value in remaining.items():
            new_lines.append(f"{key}={_format_value(value)}")

    payload = "\n".join(new_lines)
    if not payload.endswith("\n"):
        payload += "\n"

    # Atomic write so a Ctrl-C during the wizard does not corrupt the .env.
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp_name = fh.name
            fh.write(payload)
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        # If we never reached os.replace, the staged temp file is still on
        # disk — best-effort unlink. Swallow OSError so a flaky filesystem
        # cleanup does not mask the real exception that triggered finally.
        if tmp_name is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)


def quote_for_shell(value: str) -> str:
    """Convenience wrapper around ``shlex.quote`` used by wizard output."""
    return shlex.quote(value)

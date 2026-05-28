"""On-disk store for share records — full assistant turns, diffs, summaries.

Each share is a directory under ``<layer_dir>/shares/<share_id>/``
containing ``meta.json`` (kind + window_id + created_at + title) and
``content.md`` (the rendered markdown body). Adding new "kinds"
(diff, summary, pr-log) is just an enum entry on the loader side; the
store is content-agnostic.

share_id is a base32 random id — collision-free for any realistic share
volume and shorter than UUID hex.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import structlog

from ..config import layer_dir

logger = structlog.get_logger()


ShareKind = Literal["claude-turn", "summary", "diff", "pr-log", "raw"]


def shares_dir() -> Path:
    return layer_dir() / "shares"


@dataclass(frozen=True)
class ShareRecord:
    """A loaded share's metadata + body."""

    share_id: str
    kind: ShareKind
    title: str
    body_markdown: str
    window_id: str | None
    created_at: float
    meta: dict[str, object] = field(default_factory=dict)


class ShareNotFound(LookupError):
    """Raised when ``load_share(share_id)`` cannot resolve a record."""


def _new_share_id() -> str:
    """16-byte url-safe random id; ~26 chars, lower-case + digits."""
    return secrets.token_urlsafe(16).lower().replace("-", "").replace("_", "")[:24]


def save_share(
    *,
    kind: ShareKind,
    title: str,
    body_markdown: str,
    window_id: str | None = None,
    extra_meta: dict[str, object] | None = None,
) -> str:
    """Persist a share record and return its ``share_id``.

    The directory layout is intentionally simple so an operator can ``ls
    ~/.ccgram/layer/shares/`` and tell at a glance which shares exist.
    Atomic-write semantics: ``meta.json`` lands last, so a half-written
    share never appears as "complete".
    """
    share_id = _new_share_id()
    root = shares_dir() / share_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "content.md").write_text(body_markdown, encoding="utf-8")
    meta = {
        "share_id": share_id,
        "kind": kind,
        "title": title,
        "window_id": window_id,
        "created_at": time.time(),
        "extra": extra_meta or {},
    }
    (root / "meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
    )
    logger.debug("share saved: id=%s kind=%s title=%r", share_id, kind, title[:80])
    return share_id


def load_share(share_id: str) -> ShareRecord:
    """Load a share record. Raises :class:`ShareNotFound` if missing/corrupt."""
    if not share_id or "/" in share_id or ".." in share_id:
        raise ShareNotFound(f"invalid share_id: {share_id!r}")
    root = shares_dir() / share_id
    meta_path = root / "meta.json"
    body_path = root / "content.md"
    if not meta_path.is_file() or not body_path.is_file():
        raise ShareNotFound(share_id)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        body = body_path.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        raise ShareNotFound(f"corrupt share {share_id}: {exc}") from exc
    return ShareRecord(
        share_id=str(meta.get("share_id", share_id)),
        kind=str(meta.get("kind", "raw")),  # type: ignore[arg-type]
        title=str(meta.get("title", "")),
        body_markdown=body,
        window_id=meta.get("window_id"),
        created_at=float(meta.get("created_at", 0.0)),
        meta=meta.get("extra", {}) if isinstance(meta.get("extra"), dict) else {},
    )


def prune_expired(*, max_age_seconds: int = 7 * 86400) -> int:
    """Delete share directories older than ``max_age_seconds``.

    Default is 7 days — generous margin over the 3-day token TTL so a
    user with a still-valid link tomorrow doesn't hit a 404 mid-week.
    Returns the count of directories removed.
    """
    root = shares_dir()
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        meta_path = entry / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            created_at = float(meta.get("created_at", 0.0))
        except (OSError, json.JSONDecodeError, ValueError):
            # Corrupt or unreadable — fall back to mtime so we still
            # garbage-collect it eventually.
            try:
                created_at = entry.stat().st_mtime
            except OSError:
                continue
        if created_at < cutoff:
            import shutil  # Lazy: rmtree is only needed in the prune path.

            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("Pruned %d expired share record(s)", removed)
    return removed

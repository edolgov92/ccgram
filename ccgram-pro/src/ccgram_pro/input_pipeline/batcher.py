"""Per-window batch accumulator + flush logic.

Public surface:

- :func:`enqueue` — append a text or voice item to a window's batch and
  return the *index* + *total* of the new item so the caller can update
  its status reply.
- :func:`flush` — compose the buffered items into one combined message
  (with preamble on first send), clear the batch, and call the original
  ``send_to_window`` to deliver it. Returns the composed text.
- :func:`pending_count` — read the current size for status-reply rendering.

State lives entirely on the sidecar (``current_batch`` + ``preamble_sent``).
All mutations go through :func:`ccgram_pro.state.transaction` so two
arrivals 200 ms apart serialize cleanly.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from .. import state
from ..config import load_settings

logger = structlog.get_logger()


@dataclass(frozen=True)
class FlushResult:
    """Returned by :func:`flush` so the caller knows what got sent."""

    combined_text: str
    item_count: int
    preamble_included: bool


async def enqueue(
    window_id: str, *, kind: str, body: str, transcribing: bool = False
) -> tuple[int, int]:
    """Append a ``BatchItem`` for *window_id*. Returns ``(index, total)``.

    ``index`` is 1-based — convenient for the "Item 1/3" status string.
    """
    if kind not in ("text", "voice"):
        raise ValueError(f"unknown batch item kind: {kind!r}")
    async with state.transaction(window_id):
        sidecar = state.get_or_create(window_id)
        sidecar.current_batch.append(
            state.BatchItem(kind=kind, body=body, transcribing=transcribing)
        )
        total = len(sidecar.current_batch)
        state.save(sidecar)
    logger.debug(
        "batcher: enqueued %s for %s (%d items pending)", kind, window_id, total
    )
    return total, total


def pending_count(window_id: str) -> int:
    """Return the current pending-batch size without mutation."""
    sidecar = state.load(window_id)
    if sidecar is None:
        return 0
    return len(sidecar.current_batch)


def _voice_note() -> str:
    return load_settings().voice.transcription_note


def _compose(items: list[state.BatchItem], preamble: str | None) -> str:
    """Build the combined prompt text from buffered items."""
    parts: list[str] = []
    if preamble:
        parts.append(preamble.strip())
    voice_note_added = False
    for item in items:
        body = (item.body or "").strip()
        if not body:
            continue
        if item.kind == "voice":
            if not voice_note_added:
                parts.append(f"_({_voice_note()})_")
                voice_note_added = True
            parts.append(f"[voice]: {body}")
        else:
            parts.append(body)
    return "\n\n".join(parts).strip()


async def flush(window_id: str) -> FlushResult | None:
    """Compose the buffered items + preamble; clear the batch.

    Returns ``None`` when the batch is empty (the caller can show a
    "nothing to send" toast). The actual forwarding to the agent is the
    caller's job — this function just returns the text to forward, so
    the input pipeline can decide which transport (``send_to_window``,
    shell pipeline, etc.) is appropriate.
    """
    async with state.transaction(window_id):
        sidecar = state.get_or_create(window_id)
        if not sidecar.current_batch:
            return None
        items = list(sidecar.current_batch)
        include_preamble = not sidecar.preamble_sent
        preamble: str | None = None
        if include_preamble:
            preamble = load_settings().defaults.preamble
        combined = _compose(items, preamble)
        sidecar.current_batch = []
        if include_preamble:
            sidecar.preamble_sent = True
        state.save(sidecar)
    logger.info(
        "batcher: flushed %d item(s) for %s (preamble=%s)",
        len(items),
        window_id,
        include_preamble,
    )
    return FlushResult(
        combined_text=combined,
        item_count=len(items),
        preamble_included=include_preamble,
    )


async def clear(window_id: str) -> int:
    """Discard the pending batch without sending. Returns removed count."""
    async with state.transaction(window_id):
        sidecar = state.load(window_id)
        if sidecar is None or not sidecar.current_batch:
            return 0
        removed = len(sidecar.current_batch)
        sidecar.current_batch = []
        state.save(sidecar)
    logger.info("batcher: cleared %d item(s) for %s", removed, window_id)
    return removed

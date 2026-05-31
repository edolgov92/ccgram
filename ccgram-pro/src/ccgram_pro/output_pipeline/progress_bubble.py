"""Live "⚙️ Working on your request…" bubble with a growing bulleted list.

When the user sends a message in a silent-mode topic, this module posts one
Telegram message and starts a per-window asyncio task that, every few seconds,
tails the session transcript for Claude's own progress notes (the
``<!--ccgram:progress-->…<!--/ccgram:progress-->`` markers injected via the
launch system prompt — see :mod:`output_pipeline.tldr`) and appends each as a
bullet. A rotating spinner + elapsed clock in the header keeps the bubble
visibly alive between notes.

On Stop the bubble is **finalized, not deleted**: the header flips to
"✅ Completed" and the whole bullet history stays in the chat as a record of
what Claude did this turn. The final summary message is posted *below* it.

The bubble's coordinates are mirrored onto the window sidecar
(``current_progress_bubble``) so the Stop path can finalize it even if the
in-memory registry entry was lost (process restart).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from .. import state
from .tldr import extract_progress_lines

logger = structlog.get_logger()


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_TICK_INTERVAL_SECONDS = 3.0
_WORKING_HEADER = "⚙️ Working on your request…"
_DONE_HEADER = "✅ Completed"
_WAITING_HEADER = "⏳ Awaiting your answer…"
_STALE_HEADER = "⏹ Stopped"
_MAX_VISIBLE_BULLETS = 25
_TELEGRAM_LIMIT = 4096


@dataclass
class _ActiveBubble:
    chat_id: int
    thread_id: int
    message_id: int
    task: asyncio.Task[None] | None
    started_at: float
    transcript_path: str | None = None
    last_offset: int = 0
    bullets: list[str] = field(default_factory=list)


# Per-window active bubble. Keyed by window_id so concurrent topics don't
# share bubbles and so finalize-on-Stop maps cleanly from event.window_key.
_bubbles: dict[str, _ActiveBubble] = {}


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs:02d}s"


def _format_text(started_at: float, tick: int) -> str:
    elapsed = time.time() - started_at
    spinner = _SPINNER[tick % len(_SPINNER)]
    return f"{spinner} Working on your request… ({_format_elapsed(elapsed)})"


def _render(header: str, bullets: list[str]) -> str:
    """Render the header + bulleted progress list, bounded to Telegram's limit."""
    if not bullets:
        return header
    visible = bullets[-_MAX_VISIBLE_BULLETS:]

    def _build(vis: list[str]) -> str:
        hidden = len(bullets) - len(vis)
        prefix = f"… ({hidden} earlier steps)\n" if hidden > 0 else ""
        body = "\n".join(f"• {b}" for b in vis)
        return f"{header}\n\n{prefix}{body}"

    text = _build(visible)
    while len(text) > _TELEGRAM_LIMIT and len(visible) > 1:
        visible = visible[1:]
        text = _build(visible)
    return text[:_TELEGRAM_LIMIT]


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _progress_from_line(line: str) -> list[str]:
    """Extract progress notes from one assistant JSONL line (empty otherwise)."""
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return []
    role = entry.get("role") or entry.get("type", "")
    if role != "assistant":
        return []
    message = entry.get("message", {})
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return extract_progress_lines(content)
    out: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                out.extend(extract_progress_lines(str(block.get("text", ""))))
    return out


def _scan_new_progress(transcript_path: str, offset: int) -> tuple[list[str], int]:
    """Read transcript bytes past *offset*, returning (new_progress_lines, new_offset).

    Only complete lines (up to the last newline) are consumed, so a half-written
    JSONL line at EOF is re-read next tick. The offset resets to 0 if the file
    shrank (rotation/truncation). Pure + sync — call via ``asyncio.to_thread``.
    """
    try:
        with open(transcript_path, "rb") as fh:
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
    new_offset = offset + len(consumed)
    out: list[str] = []
    for raw_line in consumed.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line:
            out.extend(_progress_from_line(line))
    return out, new_offset


async def _refresh_bullets(bubble: _ActiveBubble) -> bool:
    """Pull any new progress notes into *bubble*. Returns True if bullets grew."""
    if not bubble.transcript_path:
        return False
    new_lines, new_offset = await asyncio.to_thread(
        _scan_new_progress, bubble.transcript_path, bubble.last_offset
    )
    bubble.last_offset = new_offset
    if not new_lines:
        return False
    bubble.bullets.extend(new_lines)
    return True


async def _tick_loop(window_id: str, bot: Any) -> None:
    """Edit the bubble every tick: refresh elapsed + spinner + new bullets."""
    bubble = _bubbles.get(window_id)
    if bubble is None:
        return
    tick = 1
    try:
        while True:
            await asyncio.sleep(_TICK_INTERVAL_SECONDS)
            tick += 1
            await _refresh_bullets(bubble)
            text = _render(_format_text(bubble.started_at, tick), bubble.bullets)
            try:
                await bot.edit_message_text(
                    chat_id=bubble.chat_id,
                    message_id=bubble.message_id,
                    text=text,
                )
            except Exception:  # noqa: BLE001 -- bubble must never crash the layer
                logger.debug(
                    "progress bubble edit failed for %s", window_id, exc_info=True
                )
                # Persistent edit failure (e.g. user deleted the message): drop
                # the registry entry so a later turn's start_bubble re-posts
                # instead of no-op'ing forever on a stale "active" record.
                _bubbles.pop(window_id, None)
                return
    except asyncio.CancelledError:
        return


async def _persist_bubble(
    window_id: str, *, chat_id: int, thread_id: int, message_id: int
) -> None:
    async with state.transaction(window_id):
        sidecar = state.load(window_id)
        if sidecar is None:
            return
        sidecar.current_progress_bubble = {
            "thread_id": thread_id,
            "message_id": message_id,
            "chat_id": chat_id,
        }
        state.save(sidecar)


async def _clear_persisted_bubble(window_id: str) -> None:
    async with state.transaction(window_id):
        sidecar = state.load(window_id)
        if sidecar is None or sidecar.current_progress_bubble is None:
            return
        sidecar.current_progress_bubble = None
        state.save(sidecar)


async def start_bubble(
    *,
    window_id: str,
    bot: Any,
    chat_id: int,
    thread_id: int,
    transcript_path: str | None = None,
) -> None:
    """Post a fresh bubble for *window_id* and start the tick loop.

    Idempotent for the SAME turn (e.g. rapid-fire batched flushes); but if a
    bubble from a PREVIOUS turn leaked (a turn that ended without a Stop — an
    interrupt or a dismissed question), it's finalized first so a new turn never
    stacks on a stale spinner.
    """
    if window_id in _bubbles:
        await finalize_bubble(window_id, bot, header=_STALE_HEADER)
    started_at = time.time()
    start_offset = (
        await asyncio.to_thread(_file_size, transcript_path) if transcript_path else 0
    )
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=_WORKING_HEADER,
            disable_notification=True,
        )
    except Exception:  # noqa: BLE001 -- never abort the send-flow because of UI
        logger.warning("could not post progress bubble", exc_info=True)
        return
    task = asyncio.create_task(_tick_loop(window_id, bot))
    _bubbles[window_id] = _ActiveBubble(
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=sent.message_id,
        task=task,
        started_at=started_at,
        transcript_path=transcript_path,
        last_offset=start_offset,
    )
    with contextlib.suppress(Exception):
        await _persist_bubble(
            window_id, chat_id=chat_id, thread_id=thread_id, message_id=sent.message_id
        )


async def finalize_bubble(
    window_id: str, bot: Any, *, header: str = _DONE_HEADER
) -> None:
    """Stop the tick loop and finalize the bubble.

    KEEP it (relabel header + show the bullet history) only when Claude actually
    emitted progress notes — that record is the bubble's whole point. When there
    are NO bullets the message is just noise ("✅ Completed" with nothing under
    it), so DELETE it instead. Falls back to the sidecar coordinates (delete)
    when the in-memory entry was lost across a restart.
    """
    bubble = _bubbles.pop(window_id, None)
    if bubble is not None:
        if bubble.task is not None:
            bubble.task.cancel()
            with contextlib.suppress(BaseException):
                await bubble.task
        with contextlib.suppress(Exception):
            await _refresh_bullets(bubble)
        with contextlib.suppress(Exception):
            if bubble.bullets:
                await bot.edit_message_text(
                    chat_id=bubble.chat_id,
                    message_id=bubble.message_id,
                    text=_render(header, bubble.bullets),
                )
            else:
                await bot.delete_message(
                    chat_id=bubble.chat_id, message_id=bubble.message_id
                )
        await _clear_persisted_bubble(window_id)
        return

    # No in-memory entry (restart-orphaned) — bullets are unknown, so just
    # remove the dangling message.
    sidecar = state.load(window_id)
    coords = sidecar.current_progress_bubble if sidecar else None
    if coords and "message_id" in coords and "chat_id" in coords:
        with contextlib.suppress(Exception):
            await bot.delete_message(
                chat_id=coords["chat_id"], message_id=coords["message_id"]
            )
    await _clear_persisted_bubble(window_id)


async def sweep_stale_bubbles(bot: Any) -> int:
    """Remove progress bubbles persisted from before a restart.

    After a restart the in-memory tick tasks (and their bullet history) are gone,
    so a persisted bubble would otherwise hang on "Working…" forever with no
    recoverable content — delete it. Compare-and-clear guards against a
    just-started fresh bubble (only clears if the sidecar still points at the
    same message). Returns the count swept.
    """
    swept = 0
    for sidecar in state.all_sidecars():
        coords = sidecar.current_progress_bubble
        if not coords or "message_id" not in coords or "chat_id" not in coords:
            continue
        msg_id = coords["message_id"]
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id=coords["chat_id"], message_id=msg_id)
        async with state.transaction(sidecar.window_id):
            fresh = state.load(sidecar.window_id)
            current = fresh.current_progress_bubble if fresh else None
            if fresh and current and current.get("message_id") == msg_id:
                fresh.current_progress_bubble = None
                state.save(fresh)
        swept += 1
    if swept:
        logger.info("swept %d stale progress bubble(s) on startup", swept)
    return swept


async def stop_for_interactive(window_id: str, bot: Any) -> None:
    """Finalize the bubble because Claude is now awaiting the user's input.

    Called when an AskUserQuestion / ExitPlanMode prompt is surfaced — the spinner
    must stop (Claude is blocked on the user, not working) so it never hangs on
    "Working…" while a question is pending or after it's dismissed.
    """
    await finalize_bubble(window_id, bot, header=_WAITING_HEADER)


def is_active(window_id: str) -> bool:
    """Return True when a bubble is currently running for *window_id*."""
    return window_id in _bubbles


def _reset_for_testing() -> None:
    """Cancel every running bubble + drop the registry. Test-harness only."""
    for bubble in _bubbles.values():
        if bubble.task is not None:
            bubble.task.cancel()
    _bubbles.clear()

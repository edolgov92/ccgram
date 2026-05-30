"""Per-window sidecar state — JSON files keyed by tmux window id.

Each live window owns one ``<window_id>.json`` file under
``<layer_dir>/state/``. Storing layer state in sidecar files rather than
extending ccgram's ``WindowState`` lets us track upstream cleanly and keeps
the layer's growing feature set off the ccgram window-state audit.

Stale-file detection: every sidecar carries a ``window_creation_epoch`` —
the tmux ``window_activity`` (or fallback ``time.time()`` at create) of the
window when the sidecar was first written. If the live window's creation
epoch differs (e.g. a tmux restart recycled the id), the sidecar is treated
as stale and ignored. GC runs lazily at extension startup; window teardown
also unlinks the file via the ccgram ``topic_state_registry`` hook (wired
from :mod:`ccgram_pro.extension`).

Atomic writes: a temp file beside the target is written then ``os.replace``-d
so partial JSON never appears on disk. Concurrent callers should wrap their
load-modify-save sequence in :func:`transaction` (async context manager
backed by a per-window ``asyncio.Lock``) to avoid lost-update races.

Filename safety: window ids are routed through
``ccgram.mailbox.sanitize_dir_name`` (rejects ``..``, ``/``, ``\\``;
converts ``:`` → ``=``) so a hostile id cannot escape the state directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import structlog
from ccgram.mailbox import sanitize_dir_name

from .config import state_dir

logger = structlog.get_logger()

# Module-level lock registry for the transaction() context manager. Locks are
# created lazily per window_id and never garbage-collected during the bot's
# lifetime — the population is bounded by the number of live windows, and the
# Lock objects themselves are tiny. The dict mutation in _lock_for is
# single-threaded because asyncio runs on one event loop thread.
_window_locks: dict[str, asyncio.Lock] = {}


@dataclass
class BatchItem:
    """One queued message in the user's accumulated batch."""

    kind: str  # "text" | "voice"
    body: str
    transcribing: bool = False
    received_at: float = field(default_factory=time.time)


@dataclass
class WindowSidecar:
    """All layer state for a single tmux window.

    Defaults are chosen so a brand-new sidecar is equivalent to "no
    customization". Persisted JSON is intentionally permissive on read —
    unknown keys are ignored, missing keys fall back to defaults — so
    forward-compatible schema additions don't require migrations.
    """

    window_id: str
    window_creation_epoch: float
    project_path: str | None = None
    model: str = "opus"
    reasoning: str = "extra-high"
    batch_mode: bool = True
    silent_mode: bool = True
    # "coding" | "plan" — the session's working mode, chosen in the new-session
    # picker and changeable live via the Settings panel. Distinct from
    # ``plan_mode`` below (which tracks the plan *approval* lifecycle).
    mode: str = "coding"
    # "current" | "worktree" | "clone" — how the session's working directory
    # was provisioned, chosen in the new-session picker.
    workspace_strategy: str = "current"
    # The branch the session was started from (None when not git / not chosen).
    base_branch: str | None = None
    current_batch: list[BatchItem] = field(default_factory=list)
    preamble_sent: bool = False
    plan_mode: str = "pending"  # "pending" | "entered" | "approved" | "skipped"
    current_progress_bubble: dict[str, int] | None = (
        None  # {"thread_id": .., "message_id": ..}
    )
    session_anchor_sha: str | None = None
    last_snapshot_id: str | None = None
    # Workspace bookkeeping — populated by ``ccgram_pro.workspaces.manager`` when
    # a per-session clone is created. ``workspace_path`` is the absolute path of
    # the cloned/copy workspace on disk; ``last_activity_at`` is the wall-clock
    # epoch of the most recent activity inside it (file touch, message sent),
    # consulted by the idle GC sweep. Both stay ``None`` when no workspace is
    # provisioned for the window.
    workspace_path: str | None = None
    last_activity_at: float | None = None


def _sidecar_path(window_id: str) -> Path:
    """Resolve the on-disk path for ``window_id``.

    Routes through :func:`ccgram.mailbox.sanitize_dir_name` which raises
    ``ValueError`` on anything containing ``..``, ``/``, or ``\\`` — so a
    hostile window id cannot escape ``state_dir()``.
    """
    return state_dir() / f"{sanitize_dir_name(window_id)}.json"


def _atomic_write(path: Path, payload: str) -> None:
    """Write *payload* to *path* atomically; clean up temp on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
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
        # If we never reached os.replace (write raised or rename failed), the
        # tempfile is still on disk — clean it up so leaks don't accumulate.
        if tmp_name is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)


def _serialize(sidecar: WindowSidecar) -> str:
    return json.dumps(asdict(sidecar), indent=2, sort_keys=True)


def _quarantine(path: Path, reason: str) -> None:
    """Rename a corrupt sidecar to ``.corrupt-<ts>`` so it doesn't get re-tried.

    The original file is kept for debugging; the active path is freed so
    callers see "no sidecar" rather than crashing on every load.
    """
    suffix = f".corrupt-{int(time.time())}"
    try:
        path.rename(path.with_name(path.name + suffix))
        logger.warning("Quarantined sidecar %s: %s", path, reason)
    except OSError as exc:
        logger.warning("Failed to quarantine %s: %s", path, exc)


def _deserialize(raw: str, window_id: str, path: Path) -> WindowSidecar | None:
    """Reconstruct a sidecar from JSON. Returns ``None`` on malformed input.

    On corruption the file is moved aside via :func:`_quarantine` so the next
    call returns ``None`` cleanly instead of hitting the same parse error.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _quarantine(path, f"JSON decode: {exc}")
        return None
    if not isinstance(data, dict):
        _quarantine(path, f"top-level type {type(data).__name__}, expected dict")
        return None

    batch_raw = data.get("current_batch", [])
    items: list[BatchItem] = []
    if isinstance(batch_raw, list):
        for it in batch_raw:
            if not isinstance(it, dict):
                continue
            try:
                items.append(
                    BatchItem(
                        kind=str(it.get("kind", "text")),
                        body=str(it.get("body", "")),
                        transcribing=bool(it.get("transcribing", False)),
                        received_at=float(it.get("received_at", time.time())),
                    )
                )
            except TypeError, ValueError:
                continue

    bubble_raw = data.get("current_progress_bubble")
    bubble: dict[str, int] | None = None
    if isinstance(bubble_raw, dict):
        try:
            bubble = {
                "thread_id": int(bubble_raw["thread_id"]),
                "message_id": int(bubble_raw["message_id"]),
            }
        except KeyError, TypeError, ValueError:
            bubble = None

    try:
        last_activity_raw = data.get("last_activity_at")
        last_activity_at = (
            float(last_activity_raw) if last_activity_raw is not None else None
        )
        return WindowSidecar(
            window_id=str(data.get("window_id", window_id)),
            window_creation_epoch=float(data.get("window_creation_epoch", 0.0)),
            project_path=data.get("project_path") or None,
            model=str(data.get("model", "opus")),
            reasoning=str(data.get("reasoning", "extra-high")),
            batch_mode=bool(data.get("batch_mode", True)),
            silent_mode=bool(data.get("silent_mode", True)),
            mode=str(data.get("mode", "coding")),
            workspace_strategy=str(data.get("workspace_strategy", "current")),
            base_branch=data.get("base_branch") or None,
            current_batch=items,
            preamble_sent=bool(data.get("preamble_sent", False)),
            plan_mode=str(data.get("plan_mode", "pending")),
            current_progress_bubble=bubble,
            session_anchor_sha=data.get("session_anchor_sha") or None,
            last_snapshot_id=data.get("last_snapshot_id") or None,
            workspace_path=data.get("workspace_path") or None,
            last_activity_at=last_activity_at,
        )
    except (TypeError, ValueError) as exc:
        _quarantine(path, f"reconstruct failed: {exc}")
        return None


def load(
    window_id: str, *, live_window_creation_epoch: float | None = None
) -> WindowSidecar | None:
    """Return the sidecar for ``window_id`` if it exists and is not stale.

    If ``live_window_creation_epoch`` is provided and differs from the stored
    epoch, the sidecar is unlinked and ``None`` is returned. This guards
    against tmux window-id recycling — if the same ``@N`` now points at a
    different window, prior layer state is dropped. The comparison is exact;
    callers should pass a stable epoch (e.g. tmux ``window_activity`` or the
    ``time.time()`` snapshot taken at ``get_or_create`` time).
    """
    path = _sidecar_path(window_id)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to read sidecar %s: %s", path, exc)
        return None
    sidecar = _deserialize(raw, window_id, path)
    if sidecar is None:
        return None
    if (
        live_window_creation_epoch is not None
        and sidecar.window_creation_epoch
        and sidecar.window_creation_epoch != live_window_creation_epoch
    ):
        # Transient race — tmux recycled a window id. Discard the stale
        # sidecar so the caller (get_or_create) writes a fresh one. DEBUG,
        # not INFO: window recycling is a normal event in long-running
        # sessions and would otherwise spam steady-state logs.
        logger.debug(
            "Sidecar epoch mismatch for %s (stored=%s live=%s); discarding",
            window_id,
            sidecar.window_creation_epoch,
            live_window_creation_epoch,
        )
        delete(window_id)
        return None
    return sidecar


def save(sidecar: WindowSidecar) -> None:
    """Atomically persist ``sidecar`` to disk."""
    path = _sidecar_path(sidecar.window_id)
    _atomic_write(path, _serialize(sidecar))


def delete(window_id: str) -> None:
    """Remove the sidecar for ``window_id`` if present."""
    path = _sidecar_path(window_id)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Failed to delete sidecar %s: %s", path, exc)


def get_or_create(
    window_id: str, *, live_window_creation_epoch: float | None = None
) -> WindowSidecar:
    """Load the sidecar or create a fresh one with epoch fingerprint.

    Not concurrency-safe on its own — callers that may execute concurrently
    against the same ``window_id`` should wrap the call in
    :func:`transaction`. Single-caller use (boot-time setup, doctor) is
    fine.
    """
    existing = load(window_id, live_window_creation_epoch=live_window_creation_epoch)
    if existing is not None:
        return existing
    sidecar = WindowSidecar(
        window_id=window_id,
        window_creation_epoch=(
            live_window_creation_epoch
            if live_window_creation_epoch is not None
            else time.time()
        ),
    )
    save(sidecar)
    return sidecar


def all_sidecars() -> list[WindowSidecar]:
    """Return every readable sidecar on disk. Corrupt files are quarantined."""
    results: list[WindowSidecar] = []
    d = state_dir()
    if not d.exists():
        return results
    for path in d.iterdir():
        if path.suffix != ".json" or not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Skipping unreadable sidecar %s: %s", path, exc)
            continue
        sidecar = _deserialize(raw, path.stem, path)
        if sidecar is not None:
            results.append(sidecar)
    return results


def gc_stale(live_window_ids: set[str], *, force: bool = False) -> int:
    """Delete sidecars for windows not in *live_window_ids*. Returns count removed.

    Refuses to run when *live_window_ids* is empty unless ``force=True`` is
    set — an empty set is almost always the result of a failed ``tmux
    list-windows`` query, and wiping every sidecar in response would lose
    real state. Callers that intentionally want to clear all state (test
    teardown) must opt in explicitly.
    """
    if not live_window_ids and not force:
        logger.debug(
            "gc_stale: refusing to GC against empty live set without force=True"
        )
        return 0
    removed = 0
    for sidecar in all_sidecars():
        if sidecar.window_id not in live_window_ids:
            delete(sidecar.window_id)
            removed += 1
    if removed:
        logger.info("GC'd %d stale layer sidecars", removed)
    return removed


def update(window_id: str, **changes: Any) -> WindowSidecar | None:  # noqa: ANN401 -- dataclass field setter passthrough
    """Mutate a sidecar in place and persist. Returns ``None`` if absent.

    Raises ``AttributeError`` if any *changes* key is not a sidecar field —
    writes are always local code (not user input), so a typo is a bug, not
    a migration. The race window between load and save is small but real;
    concurrent callers should wrap in :func:`transaction`.
    """
    sidecar = load(window_id)
    if sidecar is None:
        return None
    for key, value in changes.items():
        if not hasattr(sidecar, key):
            raise AttributeError(
                f"WindowSidecar has no field {key!r} (passed to update)"
            )
        setattr(sidecar, key, value)
    save(sidecar)
    return sidecar


async def update_locked(window_id: str, **changes: Any) -> WindowSidecar | None:  # noqa: ANN401 -- dataclass field setter passthrough
    """Async, race-safe :func:`update` — holds the per-window transaction lock.

    Concurrent callbacks (Settings taps, composer actions) that load-mutate-save
    the same sidecar would otherwise lose updates through the read/write window
    in :func:`update`. This wraps the same logic inside :func:`transaction` so
    only one mutation runs at a time per window.
    """
    async with transaction(window_id):
        return update(window_id, **changes)


def _lock_for(window_id: str) -> asyncio.Lock:
    """Return the lock for *window_id*, creating one on first use."""
    lock = _window_locks.get(window_id)
    if lock is None:
        lock = asyncio.Lock()
        _window_locks[window_id] = lock
    return lock


@asynccontextmanager
async def transaction(window_id: str):
    """Serialize concurrent access to the sidecar for *window_id*.

    Usage::

        async with state.transaction(window_id):
            sidecar = state.get_or_create(window_id)
            sidecar.preamble_sent = True
            state.save(sidecar)

    The lock is held for the entire ``with`` block, so callers must keep
    work inside it short. The locks live for the bot's lifetime; their
    memory cost is negligible (one ``asyncio.Lock`` per live window).
    """
    lock = _lock_for(window_id)
    async with lock:
        yield


def _reset_locks_for_testing() -> None:
    """Drop the lock registry. Tests that mutate event loops need this."""
    _window_locks.clear()

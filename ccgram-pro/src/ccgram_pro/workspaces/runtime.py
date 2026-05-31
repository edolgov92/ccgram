"""PTB JobQueue integration — schedule the workspace GC sweep.

The sweep itself is synchronous file I/O wrapped in ``asyncio.to_thread``
so the event loop stays responsive. Scheduling lives here so the
extension's ``install`` callback can wire it without depending on
``ccgram_pro.workspaces.gc`` types directly.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from ..config import load_settings
from . import gc as gc_module

if TYPE_CHECKING:
    from telegram.ext import Application, ContextTypes

logger = structlog.get_logger()


_JOB_NAME = "ccgram_pro_workspace_gc"


async def _gc_job(_context: "ContextTypes.DEFAULT_TYPE") -> None:
    """JobQueue callback — runs workspace + diff-snapshot GC off the event loop."""
    result = await asyncio.to_thread(gc_module.sweep)
    if result.total:
        logger.info(
            "Workspace GC: removed %d idle, %d orphan",
            result.idle_removed,
            result.orphans_removed,
        )
    # Prune stale diff snapshots (dirs + git refs) past the retention window.
    # Lazy: git_ops pulls subprocess; only needed inside the periodic job.
    from ..git_ops.snapshot import prune_snapshots

    prune_days = load_settings().snapshots.prune_after_days
    removed = await asyncio.to_thread(prune_snapshots, prune_after_days=prune_days)
    if removed:
        logger.info("Diff-snapshot GC: pruned %d stale window(s)", removed)


def schedule_gc(application: "Application") -> None:
    """Register the periodic GC job on *application*'s ``JobQueue``.

    No-ops gracefully when the application has no ``job_queue``
    (extensions can register their own scheduling on hosts that opted
    out of PTB's job-queue extra). Idempotent — re-registering schedules
    the next tick from now without removing the previous job, which
    matches PTB's standard pattern.
    """
    job_queue = getattr(application, "job_queue", None)
    if job_queue is None:
        logger.warning(
            "PTB JobQueue not available; ccgram-pro workspace GC will not run automatically"
        )
        return

    settings = load_settings().workspaces
    interval = settings.gc_interval_seconds
    # Stagger the first run by 60s so multiple GC tasks across extensions
    # don't all fire at startup.
    job_queue.run_repeating(_gc_job, interval=interval, first=60, name=_JOB_NAME)
    logger.info(
        "Workspace GC scheduled (interval=%ds, idle_days=%d)",
        interval,
        settings.idle_days,
    )

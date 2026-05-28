"""ccgram.extensions entry-point target — wires the layer into the bot.

Called by :func:`ccgram.bootstrap.dispatch_extensions` after the PTB
application is bootstrapped. Phase 0 is intentionally a no-op that only
logs activation and ensures the layer's state directories exist; subsequent
phases register handlers, intercept inbound and outbound messages, and
attach periodic tasks.

Contract for extension authors:

- ``install`` is called **once** per process, synchronously, with the live
  PTB ``Application``. Returning a coroutine is a bug — it will be
  discarded by :func:`ccgram.bootstrap.dispatch_extensions`.
- Registrations should be idempotent at the import level: ccgram's
  ``reset_for_testing`` may trigger a second dispatch, so prefer
  ``application.add_handler`` (additive) over module-level callback
  registration that raises on double-register.
- Faults are caught at the dispatch site; an install failure is logged but
  does not abort bot startup. Failing loudly inside ``install`` is fine
  — the bot keeps running with the layer disabled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from . import __version__
from .config import ensure_layer_dirs
from .input_pipeline import install_input_pipeline
from .output_pipeline import install_silencer, install_summarizer
from .workspaces.runtime import schedule_gc

if TYPE_CHECKING:
    from telegram.ext import Application

logger = structlog.get_logger()


def install(application: Application) -> None:
    """Install ccgram-pro into the running PTB application.

    Currently wires:

    - Layer directories exist (``state``, ``snapshots``, ``pr-loop``,
      ``workspaces``).
    - Periodic workspace GC sweep on the PTB JobQueue.
    - Output silencer that quiets ccgram's per-tick topic-emoji
      renames, status-bubble edits, and typing pings for windows whose
      sidecar has ``silent_mode = True`` (the default).
    """
    ensure_layer_dirs()
    schedule_gc(application)
    install_silencer()
    install_summarizer()
    install_input_pipeline(application)
    logger.info("ccgram-pro %s installed", __version__)

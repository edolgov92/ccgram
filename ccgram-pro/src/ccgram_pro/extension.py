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
from .git_composer import install_git_composer
from .handlers import install_layer_commands
from .input_pipeline import install_input_pipeline
from .new_session import install_new_session
from .output_pipeline import (
    install_clean_interactive,
    install_silencer,
    install_summarizer,
    progress_bubble,
)
from .plan_mode import install_plan_approval_surface
from .session_teardown import install_session_teardown
from .settings_panel import install_settings_panel
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
    install_new_session(application)
    install_settings_panel(application)
    # After the silencer (so the interactive whitelist + silent checks see the
    # wrapped chain) — augments ccgram's native ExitPlanMode prompt (fallback
    # path when the clean structured UI can't read the transcript).
    install_plan_approval_surface()
    # Clean, structured AskUserQuestion + plan prompts (replaces the scraped UI
    # for those two tools; everything else stays ccgram's).
    install_clean_interactive(application)
    install_git_composer(application)
    install_layer_commands(application)
    # Reclaim all per-window resources (workspace, snapshots, sidecar, shares)
    # when a topic is deleted / its window dies.
    install_session_teardown(application)
    # Finalize any progress bubble left spinning by a turn that was in flight
    # across this restart (its tick task died with the old process).
    bot = getattr(application, "bot", None)
    create_task = getattr(application, "create_task", None)
    if bot is not None and callable(create_task):
        create_task(progress_bubble.sweep_stale_bubbles(bot))
    logger.info("ccgram-pro %s installed", __version__)

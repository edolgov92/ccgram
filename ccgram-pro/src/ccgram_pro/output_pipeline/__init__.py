"""Output-side UX adjustments — quiet topics, "read" reactions, no rename churn.

ccgram is designed as a dense status dashboard: every Claude turn flips the
topic emoji, rewrites a status bubble with an inline keyboard, and keeps a
``typing…`` indicator pinging while the agent works. For operators who
want a normal chat UX — *my message → read mark → typing → response →
done*, no edits-in-place, no topic renames — that defaults of every
update is overwhelming.

The silencer wraps the three chatty seams in
``handlers/polling/window_tick/apply.py`` and the topic-emoji updater so
they no-op when the owning sidecar has ``silent_mode = True`` (the default
for new windows). The wrappers fall through to the originals when a
window has explicitly turned silent mode off, so power users keep the
full dashboard.

The "read" indicator is the existing ``CCGRAM_ACK_REACTION`` env var,
populated by :func:`ccgram_pro.wizard.steps`-style flows and honoured by
ccgram itself; no patching needed for that one — we just default it on.
"""

from . import progress_bubble
from .interactive_clean import install_clean_interactive
from .silencer import install_silencer
from .summarizer import install_summarizer

__all__ = [
    "install_clean_interactive",
    "install_silencer",
    "install_summarizer",
    "progress_bubble",
]

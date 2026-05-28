"""ccgram-pro — workflow layer on top of ccgram.

Activated via two entry points declared in ``pyproject.toml``:

- ``ccgram.extensions`` → :func:`ccgram_pro.extension.install` runs after the
  PTB application is bootstrapped and registers handlers, callbacks, and
  background tasks.
- ``ccgram.miniapp_factory`` → :func:`ccgram_pro.miniapp_factory.make_factory`
  wraps the default ``build_app`` so layer-owned aiohttp routes (diff viewer,
  long summaries, share links) ride the same Mini App server.

Per-window state lives at ``~/.ccgram/layer/state/<window_id>.json``. Global
defaults at ``~/.ccgram/layer/settings.toml`` and project list at
``~/.ccgram/layer/projects.toml``.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]

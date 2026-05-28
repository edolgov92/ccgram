"""Path helpers for per-window workspaces.

Mirrors :mod:`ccgram_pro.state` — window ids are routed through
``ccgram.mailbox.sanitize_dir_name`` so a hostile id (``..``, ``/``) is
rejected before it can produce a path outside :func:`workspaces_dir`.
"""

from __future__ import annotations

from pathlib import Path

from ccgram.mailbox import sanitize_dir_name

from ..config import workspaces_dir


def workspace_for_window(window_id: str) -> Path:
    """Resolve the workspace directory path for *window_id*.

    Does not create the directory — that is the manager's job. Raises
    ``ValueError`` via :func:`sanitize_dir_name` if the id contains path
    traversal sequences.
    """
    return workspaces_dir() / sanitize_dir_name(window_id)


def install_log_path(workspace: Path) -> Path:
    """Where :mod:`install` writes the captured install output."""
    return workspace / ".ccgram-install.log"

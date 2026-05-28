"""Per-session project workspaces.

Each tmux window optionally gets its own clone or copy of the chosen project
under ``<layer_dir>/workspaces/<window_id>/``, so parallel sessions on the
same source repo cannot stomp on each other (and Claude is free to modify
files without affecting the developer's main checkout).

Sub-modules:

- :mod:`paths` — directory layout helpers.
- :mod:`git_clone` — ``git clone --local --no-hardlinks`` + uncommitted /
  untracked transfer.
- :mod:`copy_strategy` — ``rsync`` (with smart excludes) / ``cp -r``
  fallback for non-git sources.
- :mod:`install` — auto-detect + run the project's package install command.
- :mod:`manager` — high-level ``create_workspace`` /
  ``delete_workspace`` orchestration. Writes ``workspace_path`` and
  ``last_activity_at`` to the sidecar.
- :mod:`gc` — sweep idle workspaces against the configured threshold.
- :mod:`runtime` — schedule the GC sweep as a PTB ``JobQueue`` task.
"""

from .manager import (
    WorkspaceCreationError,
    create_workspace,
    delete_workspace,
    touch_activity,
)

__all__ = [
    "WorkspaceCreationError",
    "create_workspace",
    "delete_workspace",
    "touch_activity",
]

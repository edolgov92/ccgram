"""Plan-mode support.

New sessions enter plan mode deterministically via the
``--permission-mode plan`` launch flag (wired in ``new_session``); there is no
Shift+Tab auto-entry orchestration. This package provides:

- :func:`install_plan_approval_surface` — augments ccgram's native
  ExitPlanMode prompt with a ⚙️ Settings button (the approval keyboard itself
  stays ccgram's, driving the real pane).
- :func:`drive_to_mode` — bounded Shift+Tab driver used by the Settings panel
  to switch a running session between plan and coding mode.
"""

from .approval_surface import install_plan_approval_surface
from .mode_control import drive_to_mode

__all__ = ["drive_to_mode", "install_plan_approval_surface"]

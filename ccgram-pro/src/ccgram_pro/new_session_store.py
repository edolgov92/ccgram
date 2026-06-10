"""Per-thread pending-new-session state — the layer's own picker state.

The new-session picker is a per-thread, multi-step flow. Riding ccgram's
*shared, per-USER* ``context.user_data`` state (``STATE_BROWSING_DIRECTORY`` +
``PENDING_THREAD_ID``) is what caused the "modal appears again and again" bug:
``_check_ui_guards`` clears that state whenever a *different* topic's thread_id
doesn't match, then falls through and re-shows the picker.

This store is keyed by ``(chat_id, thread_id)`` — globally unique per forum
topic — so cross-topic messages never collide, and the picker phase never
touches ccgram's ``STATE_KEY``. It is process-lifetime, in-memory: the picker
is ephemeral UI, and an app restart simply drops it (the next tap reports
"expired", the user resends — no half-created window, since window creation is
atomic at Start).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Picker selections older than this are considered abandoned and dropped lazily
# on the next ``get``. 30 minutes is generous for "I'll start the session in a
# bit" without leaking state for a topic the user never finished.
_PICKER_TTL_SECONDS = 1800.0

_DEFAULT_MODEL = "fable5"
_DEFAULT_EFFORT = "high"


@dataclass
class PendingSession:
    """One in-flight new-session picker, keyed by (chat_id, thread_id)."""

    chat_id: int
    thread_id: int
    user_id: int
    first_text: str
    created_at: float
    picker_message_id: int | None = None
    extra_texts: list[str] = field(default_factory=list)
    # selection
    project_idx: int = 0
    model_key: str = _DEFAULT_MODEL
    effort_key: str = _DEFAULT_EFFORT
    mode: str = "coding"  # "coding" | "plan"
    workspace_strategy: str = "current"  # "current" | "worktree" | "clone"
    # Base selection mode: "default" (switch to the repo's default branch + pull),
    # "current" (stay on the current branch), or "custom" (a branch the user picks
    # from the list — held in ``base_branch``). The safe baseline is "current";
    # ``_resolve_project_git`` PROMOTES it to "default" when a default branch is
    # detected and the tree is clean (so the picker opens on "default" as desired).
    base_mode: str = "current"
    base_branch: str | None = None  # the picked branch when base_mode == "custom"
    branch_choices: list[str] = field(default_factory=list)
    base_page: int = 0
    # Cached git status of the selected project's CURRENT checkout (computed on
    # create / project change) so the keyboard + summary never shell out per tap.
    current_branch_name: str | None = None
    default_branch_name: str | None = None
    is_dirty: bool = False  # uncommitted changes (staged/unstaged/untracked)
    has_unpushed: bool = False  # current branch ahead of its upstream
    # Cached git-ness of the selected project (computed on create / project
    # change) so the keyboard render doesn't shell out on every tap.
    project_is_git: bool = True
    # True while the base-branch sub-view is showing.
    viewing_base: bool = False
    # Set synchronously when Start begins so a double-tap can't run twice.
    in_progress: bool = False

    def combined_text(self) -> str:
        """First message plus any messages received while the picker was open."""
        parts = [self.first_text, *self.extra_texts]
        return "\n\n".join(p for p in parts if p.strip())


_pending: dict[tuple[int, int], PendingSession] = {}


def _key(chat_id: int, thread_id: int) -> tuple[int, int]:
    return (chat_id, thread_id)


def get(chat_id: int, thread_id: int) -> PendingSession | None:
    """Return the live pending session for the topic, or None.

    Lazily expires sessions older than the TTL so an abandoned picker never
    blocks a fresh start later.
    """
    session = _pending.get(_key(chat_id, thread_id))
    if session is None:
        return None
    if time.time() - session.created_at > _PICKER_TTL_SECONDS:
        clear(chat_id, thread_id)
        return None
    return session


def create(
    chat_id: int, thread_id: int, user_id: int, first_text: str, *, default_mode: str
) -> PendingSession:
    """Create and store a fresh pending session with default selection."""
    session = PendingSession(
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        first_text=first_text,
        created_at=time.time(),
        mode=default_mode,
    )
    _pending[_key(chat_id, thread_id)] = session
    return session


def append_text(chat_id: int, thread_id: int, text: str) -> None:
    """Queue an additional message that arrived while the picker was open."""
    session = _pending.get(_key(chat_id, thread_id))
    if session is not None:
        session.extra_texts.append(text)


def clear(chat_id: int, thread_id: int) -> None:
    """Drop the pending session for the topic (Start done / cancel / expiry)."""
    _pending.pop(_key(chat_id, thread_id), None)


def _reset_for_testing() -> None:
    _pending.clear()

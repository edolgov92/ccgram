"""Shared fixtures for ccgram-pro tests.

Every test gets an isolated ``CCGRAM_DIR`` pointing at a tmp_path subdir,
so the layer's state directory never touches the developer's real
``~/.ccgram/``. The layer dirs are created on demand by code under test
(via ``ensure_layer_dirs``) so fixtures stay minimal.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_ccgram_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Point CCGRAM_DIR at a per-test tmp dir so state never leaks.

    Also strips multi-instance env vars so :func:`layer_dir` doesn't
    namespace into a sub-sub-dir tests don't expect. Tests that want to
    exercise instance-name behaviour set those vars themselves.
    """
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    monkeypatch.delenv("CCGRAM_GROUP_ID", raising=False)
    monkeypatch.delenv("CCGRAM_INSTANCE_NAME", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    # state.py holds a module-level lock registry — drop it between tests so
    # an event-loop-per-test pytest setup doesn't reuse stale Locks.
    from ccgram_pro import state as _state

    _state._reset_locks_for_testing()
    yield tmp_path

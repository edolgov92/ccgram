"""Tests for ``ccgram_pro.state`` — sidecar round-trip + epoch + GC + locks."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from ccgram_pro import state
from ccgram_pro.config import ensure_layer_dirs, state_dir


def _sidecar_file(tmp_path: Path, window_id: str) -> Path:
    """Resolve the on-disk path the same way state._sidecar_path does."""
    return state._sidecar_path(window_id)


def test_save_and_load_round_trip() -> None:
    ensure_layer_dirs()
    sidecar = state.WindowSidecar(window_id="@1", window_creation_epoch=100.0)
    sidecar.model = "sonnet"
    sidecar.preamble_sent = True
    state.save(sidecar)
    loaded = state.load("@1")
    assert loaded is not None
    assert loaded.model == "sonnet"
    assert loaded.preamble_sent is True
    assert loaded.window_creation_epoch == 100.0


def test_get_or_create_creates_when_missing() -> None:
    sidecar = state.get_or_create("@2", live_window_creation_epoch=200.0)
    assert sidecar.window_id == "@2"
    assert sidecar.window_creation_epoch == 200.0
    again = state.get_or_create("@2", live_window_creation_epoch=200.0)
    assert again.window_creation_epoch == 200.0  # no rewrite


def test_load_with_matching_epoch_returns_sidecar() -> None:
    state.save(state.WindowSidecar(window_id="@3", window_creation_epoch=300.0))
    loaded = state.load("@3", live_window_creation_epoch=300.0)
    assert loaded is not None


def test_load_with_mismatched_epoch_discards() -> None:
    state.save(state.WindowSidecar(window_id="@4", window_creation_epoch=400.0))
    loaded = state.load("@4", live_window_creation_epoch=500.0)
    assert loaded is None
    assert not _sidecar_file(state_dir(), "@4").exists()


def test_load_with_zero_stored_epoch_does_not_discard() -> None:
    """A stored epoch of 0 (legacy / migrated) must not trigger mismatch."""
    state.save(state.WindowSidecar(window_id="@5", window_creation_epoch=0.0))
    loaded = state.load("@5", live_window_creation_epoch=999.0)
    assert loaded is not None


def test_load_corrupt_json_quarantines() -> None:
    ensure_layer_dirs()
    path = _sidecar_file(state_dir(), "@6")
    path.write_text("{not json")
    assert state.load("@6") is None
    assert not path.exists()
    # Quarantined sibling created.
    siblings = list(state_dir().glob("*.corrupt-*"))
    assert len(siblings) == 1


def test_load_top_level_non_dict_quarantines() -> None:
    ensure_layer_dirs()
    path = _sidecar_file(state_dir(), "@7")
    path.write_text(json.dumps(["a", "list"]))
    assert state.load("@7") is None
    siblings = list(state_dir().glob("*.corrupt-*"))
    assert len(siblings) == 1


def test_delete_idempotent() -> None:
    state.delete("@missing")  # must not raise


def test_safe_window_id_rejects_path_traversal() -> None:
    """ccgram.mailbox.sanitize_dir_name raises on '..', '/', '\\\\'."""
    with pytest.raises(ValueError):
        state.save(
            state.WindowSidecar(window_id="../escape", window_creation_epoch=0.0)
        )


def test_safe_window_id_handles_qualified_id() -> None:
    """Qualified ids like ``ccgram:@0`` should round-trip through the filename."""
    state.save(state.WindowSidecar(window_id="ccgram:@0", window_creation_epoch=0.0))
    loaded = state.load("ccgram:@0")
    assert loaded is not None


def test_all_sidecars_skips_non_json() -> None:
    ensure_layer_dirs()
    (state_dir() / "noise.txt").write_text("not json")
    state.save(state.WindowSidecar(window_id="@8", window_creation_epoch=0.0))
    sidecars = state.all_sidecars()
    assert len(sidecars) == 1
    assert sidecars[0].window_id == "@8"


def test_gc_stale_refuses_empty_set_by_default() -> None:
    state.save(state.WindowSidecar(window_id="@9", window_creation_epoch=0.0))
    removed = state.gc_stale(set())
    assert removed == 0
    assert state.load("@9") is not None  # survived


def test_gc_stale_force_wipes_everything() -> None:
    state.save(state.WindowSidecar(window_id="@10", window_creation_epoch=0.0))
    removed = state.gc_stale(set(), force=True)
    assert removed == 1


def test_gc_stale_removes_only_missing() -> None:
    state.save(state.WindowSidecar(window_id="@a", window_creation_epoch=0.0))
    state.save(state.WindowSidecar(window_id="@b", window_creation_epoch=0.0))
    removed = state.gc_stale({"@a"})
    assert removed == 1
    assert state.load("@a") is not None
    assert state.load("@b") is None


def test_update_returns_none_when_absent() -> None:
    assert state.update("@nonexistent", model="haiku") is None


def test_update_applies_changes() -> None:
    state.save(state.WindowSidecar(window_id="@u", window_creation_epoch=0.0))
    updated = state.update("@u", model="haiku", preamble_sent=True)
    assert updated is not None
    assert updated.model == "haiku"
    assert updated.preamble_sent is True
    # Persisted, not just in memory.
    again = state.load("@u")
    assert again is not None
    assert again.model == "haiku"


def test_update_raises_on_unknown_field() -> None:
    state.save(state.WindowSidecar(window_id="@bad", window_creation_epoch=0.0))
    with pytest.raises(AttributeError, match="no field 'nope'"):
        state.update("@bad", nope="oops")


def test_batch_items_round_trip() -> None:
    sidecar = state.WindowSidecar(window_id="@batch", window_creation_epoch=0.0)
    sidecar.current_batch = [
        state.BatchItem(kind="text", body="hello"),
        state.BatchItem(kind="voice", body="hi", transcribing=True),
    ]
    state.save(sidecar)
    loaded = state.load("@batch")
    assert loaded is not None
    assert len(loaded.current_batch) == 2
    assert loaded.current_batch[0].body == "hello"
    assert loaded.current_batch[1].transcribing is True


def test_progress_bubble_round_trip() -> None:
    sidecar = state.WindowSidecar(window_id="@bubble", window_creation_epoch=0.0)
    sidecar.current_progress_bubble = {"thread_id": 42, "message_id": 999}
    state.save(sidecar)
    loaded = state.load("@bubble")
    assert loaded is not None
    assert loaded.current_progress_bubble == {"thread_id": 42, "message_id": 999}


def test_atomic_write_cleans_up_temp_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If os.replace raises, the temp file must be removed."""
    ensure_layer_dirs()
    target = state_dir() / "doomed.json"

    def boom(_src: str, _dst) -> None:  # noqa: ANN001 -- match os.replace signature loosely
        raise OSError("simulated rename failure")

    monkeypatch.setattr(state.os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        state._atomic_write(target, "payload")
    assert list(state_dir().glob("doomed.json.*.tmp")) == []


async def test_transaction_serializes_concurrent_writers() -> None:
    """Two concurrent transactions on the same window_id must serialize."""
    state.save(state.WindowSidecar(window_id="@tx", window_creation_epoch=0.0))
    order: list[str] = []

    async def worker(name: str, delay: float) -> None:
        async with state.transaction("@tx"):
            order.append(f"{name}-enter")
            await asyncio.sleep(delay)
            order.append(f"{name}-exit")

    # Start a slow worker first so the fast one must wait.
    await asyncio.gather(worker("a", 0.05), worker("b", 0.0))
    # Expected: a-enter, a-exit, b-enter, b-exit (or b before a — but never
    # interleaved). Detect interleave: every -exit must come right after its
    # own -enter without the other in between.
    assert order[0].endswith("-enter")
    assert order[1] == order[0].replace("-enter", "-exit")
    assert order[2].endswith("-enter")
    assert order[3] == order[2].replace("-enter", "-exit")


async def test_transaction_lock_is_per_window() -> None:
    """Different window_ids must not block each other."""
    order: list[str] = []

    async def worker(window_id: str, delay: float) -> None:
        async with state.transaction(window_id):
            order.append(f"{window_id}-enter")
            await asyncio.sleep(delay)
            order.append(f"{window_id}-exit")

    await asyncio.gather(worker("@one", 0.05), worker("@two", 0.0))
    # @two has no delay; it should enter and exit before @one exits.
    assert "@two-exit" in order[: order.index("@one-exit")]


def test_window_sidecar_defaults_model_is_opus() -> None:
    """Phase 1 will resolve this to a Claude CLI --model arg; must be valid."""
    sidecar = state.WindowSidecar(window_id="@d", window_creation_epoch=0.0)
    assert sidecar.model == "opus"
    assert sidecar.reasoning == "extra-high"
    assert sidecar.batch_mode is True
    assert sidecar.silent_mode is True


def test_window_creation_epoch_fallback_to_time(tmp_path: Path) -> None:
    """When live epoch not supplied, get_or_create uses time.time()."""
    before = time.time()
    sidecar = state.get_or_create("@time")
    after = time.time()
    assert before - 1.0 <= sidecar.window_creation_epoch <= after + 1.0


def test_new_picker_fields_default_and_round_trip() -> None:
    sidecar = state.WindowSidecar(window_id="@n", window_creation_epoch=0.0)
    assert sidecar.mode == "coding"
    assert sidecar.workspace_strategy == "current"
    assert sidecar.base_branch is None
    sidecar.mode = "plan"
    sidecar.workspace_strategy = "worktree"
    sidecar.base_branch = "main"
    state.save(sidecar)
    loaded = state.load("@n")
    assert loaded is not None
    assert loaded.mode == "plan"
    assert loaded.workspace_strategy == "worktree"
    assert loaded.base_branch == "main"


def test_old_json_without_new_fields_deserializes_to_defaults() -> None:
    import json

    path = state._sidecar_path("@old")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"window_id": "@old", "window_creation_epoch": 0.0, "model": "opus"})
    )
    loaded = state.load("@old")
    assert loaded is not None
    assert loaded.mode == "coding"
    assert loaded.workspace_strategy == "current"
    assert loaded.base_branch is None


async def test_update_locked_persists_change() -> None:
    state.save(state.WindowSidecar(window_id="@u", window_creation_epoch=0.0))
    result = await state.update_locked("@u", mode="plan", reasoning="max")
    assert result is not None
    assert result.mode == "plan"
    loaded = state.load("@u")
    assert loaded is not None
    assert loaded.mode == "plan"
    assert loaded.reasoning == "max"

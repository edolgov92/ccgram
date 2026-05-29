"""Configuration loader — projects.toml + settings.toml + path resolution.

Layer config lives under ``<ccgram_dir>/layer/`` where ``ccgram_dir`` is
ccgram's own config directory (default ``~/.ccgram``, overridable with
``CCGRAM_DIR``). Reusing ccgram's directory keeps a single state root and
ensures backups/migrations cover both.

Subdirectories created on first run by :func:`ensure_layer_dirs`:

- ``state/``     — per-window sidecar JSONs
- ``snapshots/`` — per-iteration git diff snapshots
- ``pr-loop/``   — `/pr-fix` log files

Two TOML files (both optional):

- ``projects.toml``  — predefined project list with per-project defaults
- ``settings.toml``  — global layer defaults

Missing files yield baked-in defaults so the layer is usable out of the
box.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from ccgram.utils import ccgram_dir

logger = structlog.get_logger()


def _instance_namespace() -> str | None:
    """Return a namespace string derived from ccgram's multi-instance env vars.

    ccgram supports multiple bot instances against the same machine via
    ``CCGRAM_GROUP_ID`` (one Telegram group per bot) and
    ``CCGRAM_INSTANCE_NAME`` (display label). When either is set, the
    layer scopes its state directory by that value so two bots running
    concurrently do not stomp on each other's sidecars. ``CCGRAM_GROUP_ID``
    takes precedence because it's the partition key ccgram itself uses.
    """
    group = os.environ.get("CCGRAM_GROUP_ID", "").strip()
    if group:
        return f"group-{group}"
    instance = os.environ.get("CCGRAM_INSTANCE_NAME", "").strip()
    if instance:
        return f"instance-{instance}"
    return None


def layer_dir() -> Path:
    """Layer root directory.

    Default: ``<ccgram_dir>/layer``. When ``CCGRAM_GROUP_ID`` or
    ``CCGRAM_INSTANCE_NAME`` is set, the path becomes
    ``<ccgram_dir>/layer/<namespace>`` so multiple ccgram instances on the
    same host keep their layer state separate.
    """
    base = ccgram_dir() / "layer"
    namespace = _instance_namespace()
    return base / namespace if namespace else base


def state_dir() -> Path:
    return layer_dir() / "state"


def workspaces_dir() -> Path:
    """Root for per-window project workspaces."""
    return layer_dir() / "workspaces"


def snapshot_dir() -> Path:
    return layer_dir() / "snapshots"


def pr_loop_log_dir() -> Path:
    return layer_dir() / "pr-loop"


def projects_toml_path() -> Path:
    return layer_dir() / "projects.toml"


def settings_toml_path() -> Path:
    return layer_dir() / "settings.toml"


def ensure_layer_dirs() -> None:
    """Create all layer directories. Safe to call repeatedly."""
    for d in (
        layer_dir(),
        state_dir(),
        snapshot_dir(),
        pr_loop_log_dir(),
        workspaces_dir(),
    ):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# projects.toml
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Project:
    """One predefined project the user can pick from the /project keyboard.

    ``default_model`` accepts Claude CLI aliases (``opus``, ``sonnet``,
    ``haiku``) or full dated model ids; the value is forwarded to
    ``claude --model <model>`` at window-launch time by Phase 1.
    ``default_reasoning`` is a layer-level label (``extra-high``,
    ``high``, ``medium``, ``low``) that Phase 1 maps to a concrete
    ``--max-thinking-tokens`` value.
    ``install_command``, when set, overrides the workspace's
    auto-detected install command (``pnpm install``, ``uv sync``, …).
    Set to the empty string to skip install entirely.
    """

    path: Path
    label: str
    default_model: str = "opus"
    default_reasoning: str = "extra-high"
    default_preamble: str | None = None
    install_command: str | None = None


def load_projects(path: Path | None = None) -> list[Project]:
    """Load the predefined project list. Returns an empty list if missing."""
    if path is None:
        path = projects_toml_path()
    if not path.exists():
        return []
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("Failed to parse %s: %s", path, exc)
        return []

    items = raw.get("project", [])
    if not isinstance(items, list):
        logger.warning("projects.toml: expected [[project]] array, got %r", type(items))
        return []

    projects: list[Project] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        try:
            project_path = Path(entry["path"]).expanduser()
            label = str(entry["label"])
        except (KeyError, TypeError) as exc:
            logger.warning("projects.toml: skipping malformed entry: %s", exc)
            continue
        install_command_raw = entry.get("install_command")
        install_command = (
            str(install_command_raw) if isinstance(install_command_raw, str) else None
        )
        projects.append(
            Project(
                path=project_path,
                label=label,
                default_model=str(entry.get("default_model", "opus")),
                default_reasoning=str(entry.get("default_reasoning", "extra-high")),
                default_preamble=entry.get("default_preamble"),
                install_command=install_command,
            )
        )
    return projects


# ---------------------------------------------------------------------------
# settings.toml
# ---------------------------------------------------------------------------


_DEFAULT_PREAMBLE = (
    "Remember to follow best practices and our current project rules. We need "
    "a professional and production-ready implementation. No quick changes or "
    "hacks — proper implementation only."
)
_DEFAULT_VOICE_NOTE = (
    "Voice may have transcription errors — interpret the intent and infer "
    "what the user meant."
)


@dataclass(frozen=True)
class VoiceSettings:
    transcription_note: str = _DEFAULT_VOICE_NOTE
    flush_grace_seconds: int = 30


@dataclass(frozen=True)
class SnapshotSettings:
    prune_after_days: int = 7


@dataclass(frozen=True)
class ShareTokenSettings:
    default_ttl_seconds: int = 259200  # 3 days


_WORKSPACE_STRATEGY_CLONE = "clone"
_WORKSPACE_STRATEGY_COPY = "copy"
_WORKSPACE_STRATEGIES = frozenset({_WORKSPACE_STRATEGY_CLONE, _WORKSPACE_STRATEGY_COPY})


@dataclass(frozen=True)
class WorkspaceSettings:
    """Per-session project workspace settings.

    ``strategy`` selects the provisioning strategy:

    - ``"clone"`` (default) — ``git clone --local --no-hardlinks`` from the
      source repo, then optionally apply uncommitted edits and untracked
      files. Fast for git projects; falls back to ``"copy"`` for non-git
      sources.
    - ``"copy"`` — ``rsync`` with smart excludes
      (``node_modules``, ``.venv``, ``dist`` …) or ``cp -r`` when ``rsync``
      is unavailable. Always carries the full working state.

    ``idle_days`` is the GC threshold: workspaces whose
    ``last_activity_at`` is older than this many days are deleted on the
    next sweep.

    ``transfer_uncommitted`` controls whether the clone strategy applies
    the source repo's uncommitted diff and untracked files to the new
    workspace. Disable when sessions should always start from HEAD.

    ``install_timeout_seconds`` caps how long a ``pnpm install`` /
    ``uv sync`` / etc. run can take before being killed.

    ``gc_interval_seconds`` is the PTB JobQueue tick for the idle sweep.
    """

    strategy: str = _WORKSPACE_STRATEGY_CLONE
    idle_days: int = 5
    transfer_uncommitted: bool = True
    install_timeout_seconds: int = 600
    gc_interval_seconds: int = 3600


@dataclass(frozen=True)
class Defaults:
    silent_mode: bool = True
    batch_mode: bool = True
    plan_mode_on_new_session: bool = True
    preamble: str = _DEFAULT_PREAMBLE
    # Live "🔧 Working… (Ns)" bubble while Claude processes a turn. Off by
    # default — the 👀 ack reaction on the user's own message is enough of
    # an "I'm on it" signal, and an edit-in-place bubble reads as spam.
    progress_bubble: bool = False


@dataclass(frozen=True)
class Settings:
    defaults: Defaults = field(default_factory=Defaults)
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    snapshots: SnapshotSettings = field(default_factory=SnapshotSettings)
    share_tokens: ShareTokenSettings = field(default_factory=ShareTokenSettings)
    workspaces: WorkspaceSettings = field(default_factory=WorkspaceSettings)


def _coerce_bool(value: Any, fallback: bool) -> bool:  # noqa: ANN401 -- TOML scalar boundary
    return bool(value) if isinstance(value, bool) else fallback


def _coerce_int(value: Any, fallback: int) -> int:  # noqa: ANN401 -- TOML scalar boundary
    # bool is a subclass of int in Python; isinstance(True, int) is True. A
    # TOML author writing ``flush_grace_seconds = true`` should not silently
    # get a 1 — that's almost always a typo, so we fall back to the default.
    if isinstance(value, bool):
        return fallback
    return int(value) if isinstance(value, int) else fallback


def _coerce_str(value: Any, fallback: str) -> str:  # noqa: ANN401 -- TOML scalar boundary
    return str(value) if isinstance(value, str) else fallback


def load_settings(path: Path | None = None) -> Settings:
    """Load layer settings. Returns baked-in defaults if file missing or invalid."""
    if path is None:
        path = settings_toml_path()
    if not path.exists():
        return Settings()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("Failed to parse %s: %s — using defaults", path, exc)
        return Settings()

    d = raw.get("defaults", {}) if isinstance(raw.get("defaults"), dict) else {}
    v = raw.get("voice", {}) if isinstance(raw.get("voice"), dict) else {}
    s = raw.get("snapshots", {}) if isinstance(raw.get("snapshots"), dict) else {}
    t = raw.get("share_tokens", {}) if isinstance(raw.get("share_tokens"), dict) else {}
    w = raw.get("workspaces", {}) if isinstance(raw.get("workspaces"), dict) else {}

    defaults = Defaults(
        silent_mode=_coerce_bool(d.get("silent_mode"), True),
        batch_mode=_coerce_bool(d.get("batch_mode"), True),
        plan_mode_on_new_session=_coerce_bool(d.get("plan_mode_on_new_session"), True),
        preamble=_coerce_str(d.get("preamble"), _DEFAULT_PREAMBLE),
        progress_bubble=_coerce_bool(d.get("progress_bubble"), False),
    )
    voice = VoiceSettings(
        transcription_note=_coerce_str(
            v.get("transcription_note"), _DEFAULT_VOICE_NOTE
        ),
        flush_grace_seconds=_coerce_int(v.get("flush_grace_seconds"), 30),
    )
    snapshots = SnapshotSettings(
        prune_after_days=_coerce_int(s.get("prune_after_days"), 7),
    )
    share_tokens = ShareTokenSettings(
        default_ttl_seconds=_coerce_int(t.get("default_ttl_seconds"), 259200),
    )
    strategy_raw = _coerce_str(w.get("strategy"), _WORKSPACE_STRATEGY_CLONE)
    strategy = (
        strategy_raw
        if strategy_raw in _WORKSPACE_STRATEGIES
        else _WORKSPACE_STRATEGY_CLONE
    )
    workspaces = WorkspaceSettings(
        strategy=strategy,
        idle_days=_coerce_int(w.get("idle_days"), 5),
        transfer_uncommitted=_coerce_bool(w.get("transfer_uncommitted"), True),
        install_timeout_seconds=_coerce_int(w.get("install_timeout_seconds"), 600),
        gc_interval_seconds=_coerce_int(w.get("gc_interval_seconds"), 3600),
    )
    return Settings(
        defaults=defaults,
        voice=voice,
        snapshots=snapshots,
        share_tokens=share_tokens,
        workspaces=workspaces,
    )

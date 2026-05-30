"""``ccgram-pro doctor`` — validate the layer is installed and ready.

Checks (each printed with a ``[OK]`` / ``[WARN]`` / ``[FAIL]`` tag):

1. ``ccgram.extensions`` entry-point points at ``ccgram_pro.extension:install``.
2. ``ccgram.miniapp_factory`` entry-point points at
   ``ccgram_pro.miniapp_factory:make_factory``.
3. ccgram exposes the dispatch sites (``bootstrap.dispatch_extensions`` and
   ``main._resolve_miniapp_factory``). Missing means the host bot has not
   picked up the hook PR yet.
4. Layer directories under ``<ccgram_dir>/layer/`` are writable.
5. ``projects.toml`` parses (warn if missing — defaults still work).
6. ``settings.toml`` parses (warn if missing — defaults still work).
7. ``gh`` CLI is on ``$PATH`` (needed by Phase 7's PR review loop).

Returns 0 if every check is OK or WARN; 1 if any FAIL.
"""

from __future__ import annotations

import os
import shutil
import sys
from importlib.metadata import entry_points
from pathlib import Path
from typing import Literal

from . import __version__
from .config import (
    ensure_layer_dirs,
    layer_dir,
    load_projects,
    load_settings,
    projects_toml_path,
    settings_toml_path,
    state_dir,
    workspaces_dir,
)

Status = Literal["OK", "WARN", "FAIL"]


def _use_colors() -> bool:
    """Match ccgram's NO_COLOR / FORCE_COLOR convention.

    Presence-based per the NO_COLOR / FORCE_COLOR specs: set with any
    value (including empty) counts. NO_COLOR wins over FORCE_COLOR.
    """
    if "NO_COLOR" in os.environ:
        return False
    if "FORCE_COLOR" in os.environ:
        return True
    return bool(sys.stdout.isatty())


def _emit(status: Status, label: str, detail: str = "") -> None:
    color = {"OK": "\x1b[32m", "WARN": "\x1b[33m", "FAIL": "\x1b[31m"}[status]
    reset = "\x1b[0m"
    tag = f"{color}[{status:<4}]{reset}" if _use_colors() else f"[{status:<4}]"
    line = f"{tag} {label}"
    if detail:
        line += f" — {detail}"
    print(line)


def _check_entry_points() -> Status:
    overall: Status = "OK"
    expected = {
        "ccgram.extensions": "ccgram_pro.extension:install",
        "ccgram.miniapp_factory": "ccgram_pro.miniapp_factory:make_factory",
    }
    for group, expected_target in expected.items():
        eps = list(entry_points(group=group))
        ours = [ep for ep in eps if ep.value == expected_target]
        if not ours:
            _emit(
                "FAIL",
                f"Entry point {group} → {expected_target}",
                "missing — is ccgram-pro installed?",
            )
            overall = "FAIL"
            continue
        # WARN only when MULTIPLE ours are present (duplicate install of
        # ccgram-pro itself). Other plugins coexisting in the same group is
        # by design — Phase 5+ may add additional ccgram extensions that
        # register here, and the doctor should not nag the operator about it.
        if len(ours) > 1:
            _emit(
                "WARN",
                f"Entry point {group}",
                f"ccgram-pro registered {len(ours)}× — duplicate install",
            )
            if overall == "OK":
                overall = "WARN"
            continue
        _emit("OK", f"Entry point {group}", expected_target)
    return overall


def _load_config_env() -> None:
    """Load ``.env`` like ccgram does, so ``Config()`` can construct on import.

    Importing ``ccgram.main`` instantiates ``ccgram.config.Config`` at module
    load, which requires ``TELEGRAM_BOT_TOKEN``. ``Config.__init__`` itself
    reads the config-dir ``.env`` first — but only *after* we've already
    triggered the import. So mirror its order here (local ``.env`` wins, then
    ``<ccgram_dir>/.env``) before the import, letting ``doctor`` succeed on a
    configured host without the operator having to export the token by hand.
    """
    # Lazy: dotenv + ccgram.utils are only needed for this one check.
    from ccgram.utils import ccgram_dir

    # Lazy: dotenv only needed for this check.
    from dotenv import load_dotenv

    for env_path in (Path(".env"), ccgram_dir() / ".env"):
        if env_path.is_file():
            # override=False (default): first-loaded wins, matching ccgram.
            load_dotenv(env_path)


def _check_dispatch_sites() -> Status:
    """Confirm ccgram's host has the hook dispatch sites we depend on."""
    try:
        _load_config_env()
        # Lazy: ccgram is the host package; importing inside the check lets
        # us report a usable error if ccgram is uninstalled or partially
        # installed, instead of failing at module load.
        from ccgram import bootstrap as _bootstrap, main as _main
    except ImportError as exc:
        _emit("FAIL", "Import ccgram", str(exc))
        return "FAIL"
    except ValueError as exc:
        # ccgram.config.Config() raises ValueError when TELEGRAM_BOT_TOKEN is
        # absent. On a fresh, otherwise-correct install the operator simply
        # hasn't configured the token yet — report a clean WARN with the
        # remedy instead of crashing with a traceback (the README documents
        # `ccgram-pro doctor` as the post-install step).
        _emit(
            "WARN",
            "Import ccgram.main",
            f"config not loadable ({exc}); set TELEGRAM_BOT_TOKEN / ALLOWED_USERS "
            "in ~/.ccgram/.env, then re-run doctor",
        )
        return "WARN"

    overall: Status = "OK"
    if not hasattr(_bootstrap, "dispatch_extensions"):
        _emit(
            "FAIL",
            "ccgram.bootstrap.dispatch_extensions",
            "missing — host ccgram lacks the entry-point hook",
        )
        overall = "FAIL"
    else:
        _emit("OK", "ccgram.bootstrap.dispatch_extensions", "present")

    if not hasattr(_main, "_resolve_miniapp_factory"):
        _emit(
            "FAIL",
            "ccgram.main._resolve_miniapp_factory",
            "missing — host ccgram lacks the miniapp-factory hook",
        )
        overall = "FAIL"
    else:
        _emit("OK", "ccgram.main._resolve_miniapp_factory", "present")
    return overall


def _check_layer_dirs() -> Status:
    remedy = "set CCGRAM_DIR to a writable path or fix permissions"
    try:
        ensure_layer_dirs()
    except OSError as exc:
        _emit("FAIL", "Layer dirs", f"{layer_dir()}: {exc} — {remedy}")
        return "FAIL"

    probe = state_dir() / ".doctor-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        _emit("FAIL", f"Writable {state_dir()}", f"{exc} — {remedy}")
        return "FAIL"
    _emit("OK", f"Layer dirs under {layer_dir()}", "writable")
    return "OK"


def _check_workspaces() -> Status:
    root = workspaces_dir()
    if not root.is_dir():
        # ensure_layer_dirs() was called by _check_layer_dirs; missing now
        # would be a real filesystem fault.
        _emit("FAIL", f"Workspaces dir {root}", "missing after ensure_layer_dirs()")
        return "FAIL"
    count = sum(
        1 for entry in root.iterdir() if entry.is_dir() and ".stage-" not in entry.name
    )
    detail = (
        f"{count} workspace(s) currently provisioned" if count else "no workspaces yet"
    )
    _emit("OK", f"Workspaces dir {root}", detail)
    return "OK"


def _check_git() -> Status:
    """The default workspace strategy needs ``git`` on $PATH."""
    if shutil.which("git") is None:
        _emit(
            "WARN",
            "git CLI",
            "absent — workspace strategy will fall back to filesystem copy",
        )
        return "WARN"
    _emit("OK", "git CLI", "present")
    return "OK"


def _check_projects(path: Path) -> Status:
    if not path.exists():
        _emit(
            "WARN", f"projects.toml ({path})", "missing — /project picker will be empty"
        )
        return "WARN"
    projects = load_projects(path)
    if not projects:
        _emit("WARN", "projects.toml", "no [[project]] entries parsed")
        return "WARN"
    _emit("OK", "projects.toml", f"{len(projects)} project(s)")
    return "OK"


def _check_settings(path: Path) -> Status:
    if not path.exists():
        _emit("WARN", f"settings.toml ({path})", "missing — using baked-in defaults")
        return "WARN"
    # Loading is permissive; just confirm it doesn't fall back to defaults
    # unexpectedly.
    load_settings(path)
    _emit("OK", "settings.toml", "parsed")
    return "OK"


def _check_gh_cli() -> Status:
    """Phase 7 dependency. Not required for Phase 0, so report OK either way.

    Once ``/pr-fix`` ships we promote a missing ``gh`` to WARN — for now it
    is an informational note so operators not on the PR-loop flow don't see
    a permanent yellow flag.
    """
    if shutil.which("gh") is None:
        _emit("OK", "gh CLI", "absent — only required for Phase 7's /pr-fix")
        return "OK"
    _emit("OK", "gh CLI", "present")
    return "OK"


def _worst(*statuses: Status) -> Status:
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    return "OK"


def run_doctor() -> int:
    """Run every check, print results, return shell exit code."""
    print(f"ccgram-pro {__version__} doctor")
    print()
    statuses: list[Status] = [
        _check_entry_points(),
        _check_dispatch_sites(),
        _check_layer_dirs(),
        _check_workspaces(),
        _check_git(),
        _check_projects(projects_toml_path()),
        _check_settings(settings_toml_path()),
        _check_gh_cli(),
    ]
    print()
    overall = _worst(*statuses)
    print(f"Overall: {overall}")
    return 1 if overall == "FAIL" else 0

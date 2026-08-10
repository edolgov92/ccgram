"""Usage/limits footer appended to AI summary messages.

Fetches account rate-limit utilization (5h / weekly / per-model scoped caps like
Fable) from Claude Code's ``/api/oauth/usage`` endpoint using the Claude OAuth
token, cached so we never call it per-message, and computes the session's
context-window usage from its transcript. Every path degrades to an empty string
on any failure so a summary is never blocked or broken by a usage lookup.

Footer shape (leading token is the model that actually answered — see
``_session_model_label`` — then the Claude app's usage view):
    Fable 5 1M; Context: 43%; 5h: 6% (res 3:19pm); Weekly: 63% (res 13 Jul 1:59pm); Fable: 100% (res 13 Jul 1:59pm)
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Any

import structlog

from .model_names import display_name

logger = structlog.get_logger()

_ONE_MILLION_TOKENS = 1_000_000  # the 1M context window threshold
_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CACHE_TTL = 60.0  # seconds — usage windows move slowly; one call/min at most
_HTTP_TIMEOUT = 6.0
_HTTP_OK = 200

# Module-level last-good cache: {"at": monotonic, "data": dict | None}.
_cache: dict[str, Any] = {"at": 0.0, "data": None}
_lock = asyncio.Lock()


def _read_oauth_token() -> str | None:
    """Read the Claude Code OAuth access token from the resolved config dir."""
    # Lazy: ccgram config resolves CLAUDE_CONFIG_DIR → the credentials path.
    from ccgram.config import config

    try:
        data = json.loads(
            (config.claude_config_dir / ".credentials.json").read_text(encoding="utf-8")
        )
    except OSError, ValueError:
        return None
    token = (data.get("claudeAiOauth") or {}).get("accessToken")
    return token or None


async def _fetch_usage() -> dict[str, Any] | None:
    """GET the usage endpoint, cached ``_CACHE_TTL`` seconds. None on failure.

    On a transient error the last-good cached payload is returned if present, so a
    single blip doesn't drop the account line for a full minute.
    """
    now = time.monotonic()
    if _cache["data"] is not None and now - _cache["at"] < _CACHE_TTL:
        return _cache["data"]
    async with _lock:
        now = time.monotonic()
        if _cache["data"] is not None and now - _cache["at"] < _CACHE_TTL:
            return _cache["data"]
        token = _read_oauth_token()
        if not token:
            return _cache["data"]
        # Lazy: httpx only needed on this path.
        import httpx

        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(
                    _USAGE_URL,
                    headers={
                        "authorization": f"Bearer {token}",
                        "anthropic-beta": "oauth-2025-04-20",
                        "content-type": "application/json",
                    },
                )
            if resp.status_code != _HTTP_OK:
                logger.debug("usage fetch non-200: %s", resp.status_code)
                return _cache["data"]
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("usage fetch failed: %s", exc)
            return _cache["data"]
        _cache["data"] = data
        _cache["at"] = time.monotonic()
        return data


def _fmt_reset(iso: str | None, *, with_date: bool) -> str:
    """Format an ISO-8601 UTC reset timestamp in local time, e.g. ``3:19pm`` or
    ``13 Jul 1:59pm``. Empty on parse failure."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso).astimezone()  # -> local tz
    except ValueError, TypeError:
        return ""
    clock = dt.strftime("%-I:%M%p").lower()  # "4:45pm"
    return f"{dt.strftime('%-d %b')} {clock}" if with_date else clock


def format_account_limits(usage: dict[str, Any]) -> str:
    """Build the account portion: 5h, weekly, and any per-model weekly cap (Fable)."""
    parts: list[str] = []

    five_hour = usage.get("five_hour") or {}
    if five_hour.get("utilization") is not None:
        reset = _fmt_reset(five_hour.get("resets_at"), with_date=False)
        parts.append(
            f"5h: {round(five_hour['utilization'])}%"
            + (f" (res {reset})" if reset else "")
        )

    seven_day = usage.get("seven_day") or {}
    if seven_day.get("utilization") is not None:
        reset = _fmt_reset(seven_day.get("resets_at"), with_date=True)
        parts.append(
            f"Weekly: {round(seven_day['utilization'])}%"
            + (f" (res {reset})" if reset else "")
        )

    # Per-model weekly-scoped caps (e.g. Fable) surface as their own limit line.
    for limit in usage.get("limits") or []:
        model = (limit.get("scope") or {}).get("model") or {}
        name = model.get("display_name")
        if limit.get("group") == "weekly" and name and limit.get("percent") is not None:
            reset = _fmt_reset(limit.get("resets_at"), with_date=True)
            parts.append(
                f"{name}: {round(limit['percent'])}%"
                + (f" (res {reset})" if reset else "")
            )

    return "; ".join(parts)


def _context_window(model: str) -> int:
    """Context-window size for a model key/id. Opus 5 and Fable are natively 1M
    (as are the explicit ``[1m]`` variants); legacy Opus 4.8 without ``[1m]`` is
    200k."""
    lowered = (model or "").lower()
    if any(tag in lowered for tag in ("1m", "fable", "opus5", "opus-5")):
        return _ONE_MILLION_TOKENS
    return 200_000


def _sidecar_model(window_id: str) -> str:
    # Lazy: state is a sibling module; deferred to keep import edges minimal.
    from . import state

    sidecar = state.load(window_id)
    return (sidecar.model if sidecar else "") or ""


def _latest_turn_tokens(transcript: str) -> tuple[int, str]:
    """Return ``(context tokens, model)`` for the latest assistant turn.

    Context tokens = input + cache-read + cache-creation of that turn (what was
    actually sent to the model). ``(0, "")`` if the file is unreadable or empty.
    """
    total = 0
    model = ""
    try:
        with open(transcript, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                message = row.get("message") if row.get("type") == "assistant" else None
                if not isinstance(message, dict):
                    continue
                turn_usage = message.get("usage") or {}
                turn_total = (
                    (turn_usage.get("input_tokens") or 0)
                    + (turn_usage.get("cache_read_input_tokens") or 0)
                    + (turn_usage.get("cache_creation_input_tokens") or 0)
                )
                if turn_total:
                    total = turn_total
                    model = message.get("model") or model
    except OSError:
        return 0, ""
    return total, model


def _session_model_label(window_id: str) -> str:
    """Human label for the model that ACTUALLY answered the latest turn.

    Ground truth is the transcript's last assistant ``message.model`` — not the
    selected/launch model, which can silently differ (Claude Code falls back to
    Opus when a scoped model like Fable is rate-limited). Appends ``1M`` when the
    session runs the 1M context window. ``""`` on any failure so the footer is
    never blocked.
    """
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.window_query import view_window

    try:
        view = view_window(window_id)
    except RuntimeError:
        return ""
    transcript = getattr(view, "transcript_path", None) if view else None
    if not transcript:
        return ""
    _total, model = _latest_turn_tokens(transcript)
    sidecar = _sidecar_model(window_id)
    label = display_name(model or sidecar)
    if label and _context_window(sidecar or model) >= _ONE_MILLION_TOKENS:
        label += " 1M"
    return label


def _context_percent(window_id: str) -> int | None:
    """Percent of the context window in use by this session's latest turn."""
    # Lazy: ccgram internal — deferred to avoid a bootstrap import cycle.
    from ccgram.window_query import view_window

    try:
        view = view_window(window_id)
    except RuntimeError:
        return None
    transcript = getattr(view, "transcript_path", None) if view else None
    if not transcript:
        return None
    total, model = _latest_turn_tokens(transcript)
    if not total:
        return None
    window = _context_window(_sidecar_model(window_id) or model)
    return max(0, min(100, round(total / window * 100)))


async def build_footer(window_id: str) -> str:
    """Return ``"\\n\\n<usage line>"`` for the summary, or ``""`` on any failure.

    Never raises — a usage lookup must not block or break an AI summary.
    """
    try:
        parts: list[str] = []
        model_label = _session_model_label(window_id)
        if model_label:
            parts.append(model_label)
        context = _context_percent(window_id)
        if context is not None:
            parts.append(f"Context: {context}%")
        usage = await _fetch_usage()
        if usage:
            account = format_account_limits(usage)
            if account:
                parts.append(account)
        return ("\n\n" + "; ".join(parts)) if parts else ""
    except Exception:  # noqa: BLE001 -- footer must never crash a summary
        logger.debug("usage footer failed for %s", window_id, exc_info=True)
        return ""


def _reset_cache_for_testing() -> None:
    _cache["at"] = 0.0
    _cache["data"] = None

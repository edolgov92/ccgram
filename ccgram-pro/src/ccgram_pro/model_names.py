"""Human-readable display names for Claude model ids / picker keys.

Single source of truth shared by the Telegram usage footer (which prefixes the
model that ACTUALLY answered) and the web transcript view (per-message model
tag). The ground-truth input is the transcript's ``message.model`` id
(``claude-fable-5``, ``claude-opus-4-8``); a picker key from the sidecar
(``fable5-1m``, ``opus5``) is accepted too as a fallback.

Why this exists: the selected/launch model can silently differ from the model
that answered — Claude Code falls back to Opus when a scoped model like Fable is
rate-limited — so "what was chosen" is not "what replied". These labels always
describe the latter.
"""

from __future__ import annotations

import re

# Families we know how to name, in match-priority order (all lowercase).
_FAMILIES = (
    ("fable", "Fable"),
    ("opus", "Opus"),
    ("sonnet", "Sonnet"),
    ("haiku", "Haiku"),
)


def _strip_context_marker(lowered: str) -> str:
    """Drop the 1M context marker so it never pollutes version extraction.

    ``claude-opus-4-8[1m]`` → ``claude-opus-4-8``; ``fable5-1m`` → ``fable5``.
    The 1M-ness is surfaced separately by the footer, not baked into the name.
    """
    lowered = lowered.replace("[1m]", "")
    return re.sub(r"[-_]?1m\b", "", lowered)


def _version_from_digits(digits: list[str]) -> str:
    """``['4','8']`` → ``4.8``; ``['5']`` → ``5``; ``[]`` → ``""``."""
    if not digits:
        return ""
    if len(digits) == 1:
        return digits[0]
    return f"{digits[0]}.{digits[1]}"


def display_name(model: str | None) -> str:
    """Return a short human label like ``Fable 5`` / ``Opus 5`` / ``Opus 4.8``.

    Accepts raw transcript ids (``claude-opus-4-8``) and sidecar picker keys
    (``opus5``, ``fable5-1m``). Empty string when *model* is falsy. Unknown
    families degrade to a best-effort title-cased cleanup so a newly released
    model still renders legibly rather than blank.
    """
    if not model:
        return ""
    lowered = _strip_context_marker(model.lower())
    for token, label in _FAMILIES:
        if token in lowered:
            tail = lowered.split(token, 1)[1]
            version = _version_from_digits(re.findall(r"\d", tail))
            return f"{label} {version}".strip()
    # Unknown family: strip a leading provider prefix and title-case the rest.
    cleaned = re.sub(r"^claude-", "", lowered).replace("-", " ").replace("_", " ")
    return cleaned.strip().title()

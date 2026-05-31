"""LLM cleanup of raw voice transcripts (homophones, mis-segmentation, punctuation).

Speech-to-text on software-development dictation reliably mishears technical
terms — "modal" vs "model", "messages" vs "messengers", "JSON" vs "Jason". This
module runs the raw Whisper transcript through the configured text completer
(a cheap model such as ``gpt-5-nano``) with a tight, correction-only system
prompt, then defends against the model "answering" instead of correcting.

Everything degrades gracefully to the raw transcript: no LLM configured, a
timeout, an API error, or an output that looks like an expansion/refusal all
return the original text unchanged. The cleanup is bounded by
:data:`_CLEANUP_TIMEOUT_S` so the confirm card never stalls.
"""

from __future__ import annotations

import asyncio

import structlog

logger = structlog.get_logger()

# The user is already watching a TYPING indicator while Whisper ran; cap the
# extra wait so the confirm card always appears promptly.
_CLEANUP_TIMEOUT_S = 6.0

_SYSTEM_PROMPT = (
    "You are a transcription cleanup assistant for a software developer "
    "dictating messages to an AI coding agent. The input is a raw speech-to-text "
    "transcript of IT / software-development speech. It frequently contains "
    "homophone and mishearing errors involving technical terms. Common "
    "confusions: 'modal' vs 'model', 'messages' vs 'messengers', 'prompt' vs "
    "'prom', 'async' vs 'a sync', 'repo' vs 'repple', 'commit' vs 'come it', "
    "'Claude' vs 'cloud', 'API' vs 'a pie', 'JSON' vs 'Jason', 'cache' vs "
    "'cash', 'git' vs 'get', 'regex', 'tokens', 'endpoint', 'webhook'. "
    "Your job: return the SAME message with ONLY transcription errors fixed — "
    "correct homophones to the technically-correct term, fix obvious "
    "mis-segmented words, and add reasonable sentence punctuation and "
    "capitalization. Do NOT change the meaning, do NOT rephrase, do NOT add or "
    "remove content, do NOT answer or act on the message, do NOT add commentary "
    "or quotation marks. If the text is already clean, return it unchanged. "
    "Output ONLY the corrected transcript text."
)


def _strip_wrappers(text: str) -> str:
    """Remove a single layer of wrapping code-fence or matching quotes."""
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        inner = text[3:-3].strip()
        # Drop a leading language hint line if present.
        if "\n" in inner:
            first, rest = inner.split("\n", 1)
            if first and " " not in first.strip():
                inner = rest
        text = inner.strip()
    if len(text) > 1 and text[0] == text[-1] and text[0] in {'"', "'", "“", "”"}:
        text = text[1:-1].strip()
    return text


def _sanitize(cleaned: str, raw: str) -> str:
    """Validate the LLM output; return "" to signal 'fall back to raw'."""
    cleaned = _strip_wrappers(cleaned)
    if not cleaned:
        return ""
    # A cleanup should be roughly the same length as the input. A much longer
    # result means the model expanded/answered the dictation rather than
    # correcting it — reject and keep the raw transcript.
    if len(cleaned) > 2.5 * len(raw) + 40:
        return ""
    return cleaned


async def clean_transcript(raw: str) -> str:
    """Return *raw* with transcription errors fixed, or *raw* unchanged on any failure."""
    if not raw.strip():
        return raw
    # Lazy: llm pulls httpx + config; only needed when a transcript arrives.
    from ccgram.llm import get_text_completer

    completer = get_text_completer()
    if completer is None:
        return raw
    try:
        cleaned = await asyncio.wait_for(
            completer.complete(_SYSTEM_PROMPT, raw), timeout=_CLEANUP_TIMEOUT_S
        )
    except TimeoutError, RuntimeError, OSError, ValueError:
        logger.debug("voice cleanup failed; using raw transcript", exc_info=True)
        return raw
    except Exception:  # noqa: BLE001 -- cleanup must never break the voice flow
        logger.debug("voice cleanup raised; using raw transcript", exc_info=True)
        return raw
    return _sanitize(cleaned, raw) or raw

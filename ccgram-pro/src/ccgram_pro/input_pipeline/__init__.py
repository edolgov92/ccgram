"""Input-side pipeline — accumulate, transform, and forward user messages.

Wires three behaviours when ``WindowSidecar.batch_mode`` is on:

- ``batcher.py`` collects text and voice items into ``sidecar.current_batch``
  instead of forwarding each one immediately. A "📝 Send N item(s)" inline
  button appears under the first buffered message; tapping it flushes the
  batch as a single combined prompt.
- ``transform.py`` prepends the configured preamble on the first send of a
  session (``sidecar.preamble_sent`` flag), so Claude gets the operator's
  baseline instructions exactly once.
- ``intercept.py`` patches ``text_handler._forward_message`` and
  ``voice_callbacks._handle_send`` so both code paths route through the
  batcher.
"""

from .intercept import install_input_pipeline

__all__ = ["install_input_pipeline"]

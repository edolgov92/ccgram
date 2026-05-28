"""Thin re-export of the silencer's window-scope check.

Kept in the input-pipeline package so :mod:`intercept` does not need to
reach across into the output pipeline's private module surface.
"""

from __future__ import annotations

from ..output_pipeline.silencer import _is_silent_for_window as is_silent_for_window

__all__ = ["is_silent_for_window"]

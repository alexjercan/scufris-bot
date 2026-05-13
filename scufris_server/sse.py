"""Server-Sent Events helpers.

We frame events manually via ``StreamingResponse`` rather than pulling
in ``sse-starlette`` — the protocol is small, the dep would be one
more thing to vendor in nix, and we already need a custom queue plumbing
anyway.

Wire format (per the design doc):

    event: thinking
    data: {"...": "..."}

    event: done
    data: {"text": "..."}

    event: error
    data: {"error": "..."}

A single comment line ``: keepalive`` is sent every ``KEEPALIVE_SECONDS``
to keep idle connections from being closed by intermediaries.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

KEEPALIVE_SECONDS: float = 15.0


def format_event(event: str, data: Mapping[str, Any]) -> bytes:
    """Format a single SSE message as bytes ready to write to the wire."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def keepalive_frame() -> bytes:
    return b": keepalive\n\n"

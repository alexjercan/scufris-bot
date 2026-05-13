"""HTTP client for the Scufris server.

A thin wrapper over :mod:`httpx` that mirrors the daemon's REST surface
(see ``tasks/20260510-192350/DESIGN.md``). Used by the local CLI today;
the future TUI / Telegram bridge / web UI all share the same client.
"""

from .client import (
    ScufrisAuthError,
    ScufrisClient,
    ScufrisConnectionError,
    ScufrisError,
    ScufrisServerError,
    StreamEvent,
    parse_sse_stream,
    user_id_for,
)

__all__ = [
    "ScufrisClient",
    "ScufrisError",
    "ScufrisAuthError",
    "ScufrisConnectionError",
    "ScufrisServerError",
    "StreamEvent",
    "parse_sse_stream",
    "user_id_for",
]

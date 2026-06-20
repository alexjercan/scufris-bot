"""Async HTTP client for the v2 scufris-server.

A thin wrapper over :mod:`httpx` that mirrors every live endpoint
under ``/v1/*`` except the non-streaming ``POST /v1/chat`` (which is
intentionally omitted — see :doc:`#14 D2 <../tasks/20260613-091049/TASK>`).

The package is a faithful descendant of v1's ``scufris_client/``
(``feature/opencode`` branch) trimmed to the v2 endpoint set and
restructured around the v2 channels-split request shape. Two consumer
surfaces today: the :mod:`scufris_cli` REPL (first-party) and any
ad-hoc script that needs to drive the server programmatically.

Typical usage::

    from scufris_client import ScufrisClient

    async with ScufrisClient(base_url="http://127.0.0.1:7080") as client:
        await client.healthz()
        resolved = await client.resolve_identity("cli", "alex")
        async for event in client.chat_stream(
            surface="cli",
            surface_id="alex",
            agent="build",
            message="hello",
        ):
            ...

The four exception classes form a flat hierarchy under
:class:`ScufrisError`; callers typically catch the specific subtype
they care about (``ScufrisConnectionError`` for "server is down" UX,
etc.) and fall back to :class:`ScufrisError` for everything else.
"""

from scufris_client.client import (
    ScufrisAuthError,
    ScufrisClient,
    ScufrisConnectionError,
    ScufrisError,
    ScufrisServerError,
)

__all__ = [
    "ScufrisAuthError",
    "ScufrisClient",
    "ScufrisConnectionError",
    "ScufrisError",
    "ScufrisServerError",
]

"""Outbound Server-Sent Events framing helpers (#11 step 6).

We frame SSE events manually rather than pulling in ``sse-starlette``:
the protocol is small (RFC EventSource: ``event:`` / ``data:`` /
comment lines, blank line as record separator), the dep would add a
nix flake input and a runtime requirement for one helper, and we
already need custom interleaving (per-session queue + keepalive
timer) that ``sse-starlette``'s abstractions would fight rather
than help.

Carried forward from v1's ``feature/opencode:scufris_server/sse.py``
which used the same hand-rolled approach. v2 adds
:func:`stream_with_keepalive` — a generic interleaver that v1
inlined inside ``routes/chat.py``. Lifted to the module so the
streaming route handler (:mod:`scufris_server.routes.chat_stream`,
step 8 of #11) stays short and a future second SSE route can reuse
the same keepalive policy.

Wire format
-----------
The framing produced by these helpers::

    event: thinking
    data: {"kind":"text","source":"scufris","text":"hi","depth":0}

    event: done
    data: {"type":"done","message":"...","oc_session_id":"ses_..."}

    event: error
    data: {"error":"..."}

A single SSE comment line ``: keepalive`` is sent every
:data:`KEEPALIVE_SECONDS` seconds when no event is ready, to keep
idle connections from being closed by intermediaries (Nginx default
60s, Cloudflare 100s, etc. — 15s leaves plenty of headroom).

JSON encoding is compact (no whitespace) and preserves Unicode
(``ensure_ascii=False``) — v1 clients (CLI / Telegram) round-trip
non-ASCII correctly.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

#: Seconds between keepalive frames when no real event is ready.
#: Chosen well below the most aggressive common intermediary
#: timeout (Nginx ``proxy_read_timeout`` 60s default) so a quiet
#: connection survives indefinitely. Matches v1.
KEEPALIVE_SECONDS: float = 15.0


def format_event(event: str, data: Mapping[str, Any]) -> bytes:
    """Format a single SSE message as bytes ready to write to the wire.

    Args:
        event: The SSE event type (the ``event:`` line value). For
            scufris this is always one of ``"thinking"``, ``"done"``,
            or ``"error"`` per the design doc; the helper takes any
            string to keep the wire layer transport-agnostic.
        data: JSON-serialisable mapping. Encoded with compact
            separators (no spaces) and ``ensure_ascii=False`` so
            non-ASCII (e.g. user prompts in any language) survives
            the round-trip.

    Returns:
        Bytes terminated by the SSE record-separator blank line.
        Safe to write directly to FastAPI ``StreamingResponse``.
    """
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode()


def format_keepalive() -> bytes:
    """Format the keepalive frame as an SSE comment line.

    SSE comment lines start with ``:`` and are ignored by all
    spec-compliant clients (including the EventSource API and
    every Python SSE consumer we've checked, plus v1's CLI
    parser at ``feature/opencode:scufris_client/client.py:141``).
    Using a comment frame instead of a no-op ``event: keepalive``
    means we don't pollute the consumer's event handler.

    The frame is constant — returned as a fresh bytes object each
    call so callers can mutate (they shouldn't, but the API is
    bytes-mutable so we don't share a reference).
    """
    return b": keepalive\n\n"


async def stream_with_keepalive(
    source: AsyncIterator[bytes],
    *,
    interval: float = KEEPALIVE_SECONDS,
) -> AsyncIterator[bytes]:
    """Interleave a source byte-stream with periodic keepalive frames.

    Yields every chunk from ``source`` in order. Whenever ``source``
    has been idle for ``interval`` seconds (no new chunk available),
    yields one :func:`format_keepalive` frame and resumes waiting.
    Stops cleanly when ``source`` raises :class:`StopAsyncIteration`.

    Args:
        source: An async iterator of bytes — typically the inner
            event-pump coroutine of a streaming route handler
            (see :mod:`scufris_server.routes.chat_stream`).
        interval: Seconds between keepalive frames during idle.
            Defaults to :data:`KEEPALIVE_SECONDS`.

    Yields:
        Bytes chunks: either the next item from ``source`` or a
        keepalive frame.

    Notes
    -----
    Implementation uses a long-lived ``next_task`` wrapped in
    :func:`asyncio.shield` for each :func:`asyncio.wait_for` call.
    The shield is load-bearing: without it, a timeout cancels the
    awaited coroutine, which in the async-generator case kills the
    generator entirely (the pending ``__anext__()`` is interrupted
    mid-flight and subsequent calls raise :class:`StopAsyncIteration`).
    With the shield, only the *wait* is interrupted on timeout —
    the underlying task keeps running, and the next loop iteration
    re-awaits the same task, picking up the chunk once it arrives.
    The task is only re-created after a successful yield.

    On consumer cancellation (route handler client disconnect),
    the ``finally`` block cancels any in-flight ``next_task`` to
    avoid leaking it.
    """
    iterator = source.__aiter__()

    async def _pull_one() -> bytes:
        # Wrapper so ``create_task`` sees a ``Coroutine[Any, Any, bytes]``
        # rather than the bare ``Awaitable[bytes]`` returned by
        # ``__anext__()``. Functionally identical; satisfies
        # ``mypy --strict``'s narrower argument type for create_task.
        return await iterator.__anext__()

    next_task: asyncio.Task[bytes] | None = None
    try:
        while True:
            if next_task is None:
                next_task = asyncio.create_task(_pull_one())
            try:
                chunk = await asyncio.wait_for(
                    asyncio.shield(next_task), timeout=interval
                )
            except TimeoutError:
                yield format_keepalive()
                continue
            except StopAsyncIteration:
                next_task = None
                return
            # Successful chunk — clear the slot for a fresh task.
            next_task = None
            yield chunk
    finally:
        if next_task is not None and not next_task.done():
            next_task.cancel()
            try:
                await next_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

"""Tests for :mod:`scufris_server.sse` (#11 step 6).

Covers the three module-level helpers:

- :data:`KEEPALIVE_SECONDS` — value pin (consumer relies on it
  being well under 60 s).
- :func:`format_event` — wire shape, compact JSON, Unicode.
- :func:`format_keepalive` — SSE comment frame.
- :func:`stream_with_keepalive` — pass-through, idle-keepalive
  injection, and clean termination on source exhaustion.

The stream tests use short ``interval`` overrides (50 ms) so the
test stays sub-second; the real ``KEEPALIVE_SECONDS=15`` is too
slow for unit tests but is asserted as a constant separately.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from scufris_server.sse import (
    KEEPALIVE_SECONDS,
    format_event,
    format_keepalive,
    stream_with_keepalive,
)

# ---------------------------------------------------------------------------
# Constant
# ---------------------------------------------------------------------------


def test_keepalive_seconds_is_fifteen() -> None:
    """Locks the keepalive cadence below the most aggressive common
    intermediary timeout (Nginx ``proxy_read_timeout`` 60 s).
    Changing this value is a wire-contract change — bump deliberately.
    """
    assert KEEPALIVE_SECONDS == 15.0


# ---------------------------------------------------------------------------
# format_event()
# ---------------------------------------------------------------------------


def test_format_event_basic_shape() -> None:
    """Single event encodes to ``event: <type>\\ndata: <json>\\n\\n``
    with one trailing blank line (SSE record separator).
    """
    out = format_event("thinking", {"kind": "text", "text": "hi"})
    # Bytes — safe for StreamingResponse.
    assert isinstance(out, bytes)
    text = out.decode()
    # Structure: event line, data line, blank line terminator.
    lines = text.split("\n")
    assert lines[0] == "event: thinking"
    assert lines[1].startswith("data: ")
    assert lines[2] == ""
    assert lines[3] == ""  # trailing newline from "\n\n"


def test_format_event_uses_compact_json() -> None:
    """JSON payload uses ``(",", ":")`` separators — no whitespace
    after commas or colons. Keeps the wire small over long-running
    SSE streams.
    """
    out = format_event("done", {"a": 1, "b": "two"})
    # If we had default separators we'd see "a": 1 with a space.
    assert b'"a":1' in out
    assert b'"b":"two"' in out
    assert b'"a": 1' not in out


def test_format_event_preserves_unicode_with_ensure_ascii_false() -> None:
    """Non-ASCII characters (user prompts in any language, emoji,
    smart quotes from copy-paste) round-trip as UTF-8 — they are NOT
    escaped to ``\\uXXXX``. v1 CLI / Telegram clients render them
    directly.
    """
    out = format_event("thinking", {"text": "héllo 你好 🐍"})
    assert "héllo 你好 🐍".encode() in out
    # No unicode escapes in the payload.
    assert b"\\u" not in out


def test_format_event_done_payload_shape_matches_design() -> None:
    """A representative ``done`` event with the full #11 D7 shape
    encodes correctly. Smoke test for the wire-format contract the
    step 8 route handler will emit.
    """
    payload = {
        "type": "done",
        "message": "reply text",
        "oc_session_id": "ses_abc",
        "oc_message_id": "msg_def",
        "tokens": {"input": 12, "output": 34},
        "cost": 0.001,
    }
    out = format_event("done", payload)
    # Parse the data line back and assert we get an identical dict.
    text = out.decode()
    data_line = next(line for line in text.split("\n") if line.startswith("data: "))
    parsed = json.loads(data_line[len("data: ") :])
    assert parsed == payload


# ---------------------------------------------------------------------------
# format_keepalive()
# ---------------------------------------------------------------------------


def test_format_keepalive_is_sse_comment_frame() -> None:
    """Keepalive uses an SSE comment line (``:`` prefix) followed by
    the record-separator blank line. Comment lines are ignored by
    every spec-compliant SSE client — they keep the TCP connection
    warm without firing the consumer's event handler.
    """
    assert format_keepalive() == b": keepalive\n\n"


# ---------------------------------------------------------------------------
# stream_with_keepalive()
# ---------------------------------------------------------------------------


async def _aiter_from_list(items: list[bytes]) -> AsyncIterator[bytes]:
    """Helper: yield each item then stop. No artificial delay."""
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_stream_with_keepalive_passes_through_and_terminates() -> None:
    """Source yields three chunks and exits; the wrapper yields the
    same three chunks in order and stops cleanly. Asserts both the
    pass-through contract and clean ``StopAsyncIteration`` handling
    in one pass.
    """
    source = _aiter_from_list([b"one", b"two", b"three"])
    out: list[bytes] = []
    async for chunk in stream_with_keepalive(source, interval=10.0):
        out.append(chunk)
    assert out == [b"one", b"two", b"three"]


@pytest.mark.asyncio
async def test_stream_with_keepalive_injects_keepalive_on_idle() -> None:
    """When the source goes idle for ``interval`` seconds, the
    wrapper yields a keepalive frame and keeps waiting. Then when a
    real chunk arrives, it passes through. Stops cleanly when the
    source exhausts.

    Uses an :class:`asyncio.Queue` as the source so we control the
    timing precisely. Tight ``interval=0.05`` keeps the test fast.
    """
    q: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def _source() -> AsyncIterator[bytes]:
        while True:
            item = await q.get()
            if item is None:
                return
            yield item

    # Feeder: wait long enough to trigger keepalive, then put one
    # real chunk, then signal end of stream.
    async def _feed() -> None:
        await asyncio.sleep(0.15)  # 3x interval — guarantees keepalive
        await q.put(b"real")
        await q.put(None)

    feed_task = asyncio.create_task(_feed())
    try:
        out: list[bytes] = []
        async for chunk in stream_with_keepalive(_source(), interval=0.05):
            out.append(chunk)
        # At least one keepalive plus the real chunk.
        assert b": keepalive\n\n" in out
        assert b"real" in out
        # Real chunk arrives AFTER the first keepalive (timing).
        real_idx = out.index(b"real")
        ka_idx = out.index(b": keepalive\n\n")
        assert ka_idx < real_idx
    finally:
        await feed_task

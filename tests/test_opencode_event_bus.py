"""Tests for the shared OpenCode ``/event`` listener (task 105013).

The bus collapses N concurrent ``GET /event`` connections down to 1
shared upstream connection by routing events to per-session
:class:`asyncio.Queue` instances. Tests cover:

- Pure-dispatch routing (no httpx): verifies queue management,
  sessionID filtering, sentinel broadcast.
- Lifecycle (start/stop idempotency, aclose chaining).
- Reconnect: simulated upstream drop emits :class:`_BusReconnected`
  to every live subscriber.
- Concurrency acceptance criterion: K parallel ``chat_stream`` calls
  share a single upstream ``GET /event`` connection.

We never spin up a real ``opencode serve`` here — a custom
:class:`httpx.AsyncBaseTransport` returns scripted streams so the
tests run in milliseconds and don't depend on the local daemon.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
import pytest

from utils.opencode_client import (
    OpenCodeClient,
    OpenCodeSessionError,
    _BusReconnected,
    _OpenCodeEventBus,
)

# ---------------------------------------------------------------------------
# Test transport
# ---------------------------------------------------------------------------


class _ScriptedSSEStream(httpx.AsyncByteStream):
    """An :class:`AsyncByteStream` whose body is fed line-by-line.

    Tests push complete SSE frames (``data: {...}\\n\\n``) via
    :meth:`emit`. :meth:`close_clean` ends the stream as if the
    server hung up gracefully; :meth:`close_with_exception` raises
    inside ``__aiter__`` to simulate a network error.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self._error: Optional[BaseException] = None
        self.aclose_called = False

    def emit(self, payload: Dict[str, Any]) -> None:
        """Emit one SSE event frame."""
        line = f"data: {json.dumps(payload)}\n\n".encode("utf-8")
        self._queue.put_nowait(line)

    def close_clean(self) -> None:
        """End the stream cleanly (server hangs up)."""
        self._queue.put_nowait(None)

    def close_with_exception(self, exc: BaseException) -> None:
        """Raise ``exc`` from ``__aiter__`` (simulates a network drop)."""
        self._error = exc
        self._queue.put_nowait(None)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                if self._error is not None:
                    raise self._error
                return
            yield chunk

    async def aclose(self) -> None:
        self.aclose_called = True


class _BusTransport(httpx.AsyncBaseTransport):
    """Fake transport: GET /event returns scripted streams, others 200/{}.

    Each ``GET /event`` call pops the next stream from
    :attr:`pending_streams`. If the deque is empty a stub stream is
    minted so the bus's reader doesn't crash — tests that care about
    a specific stream order pre-populate the deque.
    """

    def __init__(self) -> None:
        self.pending_streams: List[_ScriptedSSEStream] = []
        self.event_connect_count = 0
        self.post_calls: List[Dict[str, Any]] = []

    def queue_stream(self) -> _ScriptedSSEStream:
        """Pre-script the next ``GET /event`` response and return it."""
        stream = _ScriptedSSEStream()
        self.pending_streams.append(stream)
        return stream

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/event":
            self.event_connect_count += 1
            if self.pending_streams:
                stream = self.pending_streams.pop(0)
            else:
                stream = _ScriptedSSEStream()
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )
        if request.method == "POST" and path.startswith("/session/"):
            body = (
                json.loads(request.content.decode("utf-8")) if request.content else {}
            )
            self.post_calls.append({"path": path, "body": body})
            return httpx.Response(200, json={"info": {}, "parts": []})
        return httpx.Response(200, json={})


def _async_client_with(transport: _BusTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://test-opencode",
        transport=transport,
    )


# ---------------------------------------------------------------------------
# Pure dispatch tests (no httpx)
# ---------------------------------------------------------------------------


def _make_bus_no_httpx() -> _OpenCodeEventBus:
    """Build a bus without a real HTTP client.

    These tests don't call :meth:`start` — they exercise the
    dispatch / subscribe machinery directly.
    """
    # The constructor only stores the client reference; never used
    # unless start() runs. Pass a stub.
    return _OpenCodeEventBus(client=None, connect_timeout=0.5)  # type: ignore[arg-type]


def _evt(etype: str, session_id: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    """Helper: build a fake event dict in the live wire format."""
    props: Dict[str, Any] = {**extra}
    if session_id is not None:
        props["sessionID"] = session_id
    return {"id": f"evt_{etype}", "type": etype, "properties": props}


def test_dispatch_routes_to_session_subscriber() -> None:
    """Events for a subscribed sessionID land in that subscriber's queue."""

    async def go() -> None:
        bus = _make_bus_no_httpx()
        # Pretend we're connected so subscribe() doesn't block.
        bus._first_connected.set()  # noqa: SLF001
        async with bus.subscribe("ses_a") as q:
            bus._dispatch(_evt("session.idle", "ses_a"))  # noqa: SLF001
            assert q.qsize() == 1
            event = await q.get()
            assert event["type"] == "session.idle"

    asyncio.run(go())


def test_dispatch_drops_events_without_session_id() -> None:
    """Global events (server.connected, lsp.*) never reach subscribers."""

    async def go() -> None:
        bus = _make_bus_no_httpx()
        bus._first_connected.set()  # noqa: SLF001
        async with bus.subscribe("ses_a") as q:
            bus._dispatch(_evt("server.connected"))  # noqa: SLF001
            bus._dispatch(_evt("installation.updated"))  # noqa: SLF001
            assert q.qsize() == 0

    asyncio.run(go())


def test_dispatch_drops_events_for_unsubscribed_sessions() -> None:
    """Events for sessions nobody's subscribed to are silently dropped."""

    async def go() -> None:
        bus = _make_bus_no_httpx()
        bus._first_connected.set()  # noqa: SLF001
        async with bus.subscribe("ses_a") as q:
            bus._dispatch(_evt("session.idle", "ses_b"))  # noqa: SLF001
            assert q.qsize() == 0

    asyncio.run(go())


def test_multiple_subscribers_same_session_both_receive() -> None:
    """Two subscribers for the same session each get a copy of the event."""

    async def go() -> None:
        bus = _make_bus_no_httpx()
        bus._first_connected.set()  # noqa: SLF001
        async with bus.subscribe("ses_a") as q1:
            async with bus.subscribe("ses_a") as q2:
                bus._dispatch(_evt("message.part.delta", "ses_a", delta="hi"))  # noqa: SLF001
                assert q1.qsize() == 1
                assert q2.qsize() == 1

    asyncio.run(go())


def test_multiple_subscribers_different_sessions_routed() -> None:
    """Two subscribers for different sessions each get only their events."""

    async def go() -> None:
        bus = _make_bus_no_httpx()
        bus._first_connected.set()  # noqa: SLF001
        async with bus.subscribe("ses_a") as qa:
            async with bus.subscribe("ses_b") as qb:
                bus._dispatch(_evt("session.idle", "ses_a"))  # noqa: SLF001
                bus._dispatch(_evt("session.idle", "ses_b"))  # noqa: SLF001
                bus._dispatch(_evt("session.idle", "ses_c"))  # noqa: SLF001
                assert qa.qsize() == 1
                assert qb.qsize() == 1
                a = await qa.get()
                b = await qb.get()
                assert a["properties"]["sessionID"] == "ses_a"
                assert b["properties"]["sessionID"] == "ses_b"

    asyncio.run(go())


def test_subscribe_unsubscribes_on_exit() -> None:
    """Leaving the subscribe block removes the queue from the dispatch table."""

    async def go() -> None:
        bus = _make_bus_no_httpx()
        bus._first_connected.set()  # noqa: SLF001
        async with bus.subscribe("ses_a"):
            assert "ses_a" in bus._subs  # noqa: SLF001
        # After exit the entry is gone (last queue removed → key dropped).
        assert "ses_a" not in bus._subs  # noqa: SLF001

    asyncio.run(go())


def test_subscribe_keeps_session_key_when_one_subscriber_leaves() -> None:
    """If two subscribe to a session and one leaves, the other stays."""

    async def go() -> None:
        bus = _make_bus_no_httpx()
        bus._first_connected.set()  # noqa: SLF001
        async with bus.subscribe("ses_a") as outer:
            async with bus.subscribe("ses_a"):
                pass
            # Inner left, outer's still here.
            assert "ses_a" in bus._subs  # noqa: SLF001
            assert len(bus._subs["ses_a"]) == 1  # noqa: SLF001
            bus._dispatch(_evt("session.idle", "ses_a"))  # noqa: SLF001
            assert outer.qsize() == 1

    asyncio.run(go())


def test_broadcast_reaches_all_queues() -> None:
    """Sentinels (e.g. _BusReconnected) hit every live queue."""

    async def go() -> None:
        bus = _make_bus_no_httpx()
        bus._first_connected.set()  # noqa: SLF001
        async with bus.subscribe("ses_a") as qa:
            async with bus.subscribe("ses_b") as qb:
                await bus._broadcast(_BusReconnected())  # noqa: SLF001
                assert qa.qsize() == 1
                assert qb.qsize() == 1
                a = await qa.get()
                b = await qb.get()
                assert isinstance(a, _BusReconnected)
                assert isinstance(b, _BusReconnected)

    asyncio.run(go())


def test_stats_reflects_subscribers_and_state() -> None:
    """Bus stats counts subscribers, sessions, queue depth, reconnects."""

    async def go() -> None:
        bus = _make_bus_no_httpx()
        bus._first_connected.set()  # noqa: SLF001
        bus._connected = True  # noqa: SLF001
        bus._reconnect_count = 3  # noqa: SLF001
        async with bus.subscribe("ses_a") as qa:
            async with bus.subscribe("ses_a"):
                async with bus.subscribe("ses_b"):
                    bus._dispatch(_evt("session.idle", "ses_a"))  # noqa: SLF001
                    bus._dispatch(_evt("session.idle", "ses_a"))  # noqa: SLF001
                    stats = bus.stats
                    assert stats["connected"] is True
                    assert stats["subscribers"] == 3
                    assert stats["sessions"] == 2
                    assert stats["reconnects"] == 3
                    # qa got 2 events; the other ses_a queue too.
                    assert stats["max_queue_depth"] == 2
                    # Drain qa for cleanness.
                    while not qa.empty():
                        qa.get_nowait()

    asyncio.run(go())


def test_subscribe_times_out_when_bus_never_connects() -> None:
    """If the bus never connects, subscribe raises after connect_timeout."""

    async def go() -> None:
        bus = _make_bus_no_httpx()  # connect_timeout=0.5
        with pytest.raises(OpenCodeSessionError, match="not connected"):
            async with bus.subscribe("ses_a"):
                pass

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Lifecycle tests (with httpx transport)
# ---------------------------------------------------------------------------


def test_start_is_idempotent() -> None:
    """Calling start() twice reuses the same task."""

    async def go() -> None:
        transport = _BusTransport()
        # Pre-script one stream so the reader doesn't churn.
        stream = transport.queue_stream()
        async with _async_client_with(transport) as client:
            bus = _OpenCodeEventBus(client, connect_timeout=2.0)
            await bus.start()
            t1 = bus._task  # noqa: SLF001
            await bus.start()
            t2 = bus._task  # noqa: SLF001
            assert t1 is t2
            stream.close_clean()
            await bus.stop()

    asyncio.run(go())


def test_stop_is_idempotent_when_never_started() -> None:
    """stop() on a never-started bus is a no-op."""

    async def go() -> None:
        bus = _make_bus_no_httpx()
        await bus.stop()  # should not raise
        await bus.stop()  # twice

    asyncio.run(go())


def test_run_loop_dispatches_real_events() -> None:
    """End-to-end: bus connects, parses SSE lines, dispatches by sessionID."""

    async def go() -> None:
        transport = _BusTransport()
        stream = transport.queue_stream()
        async with _async_client_with(transport) as client:
            bus = _OpenCodeEventBus(client, connect_timeout=2.0)
            await bus.start()
            try:
                async with bus.subscribe("ses_x") as q:
                    stream.emit(_evt("message.part.delta", "ses_x", delta="hi"))
                    stream.emit(_evt("session.idle", "ses_x"))
                    # Other-session event must NOT land here.
                    stream.emit(_evt("session.idle", "ses_y"))
                    # Pull what we expect.
                    e1 = await asyncio.wait_for(q.get(), timeout=2.0)
                    e2 = await asyncio.wait_for(q.get(), timeout=2.0)
                    assert e1["type"] == "message.part.delta"
                    assert e2["type"] == "session.idle"
                    # Give the loop a chance to dispatch the ses_y
                    # event then prove our queue is empty.
                    await asyncio.sleep(0.05)
                    assert q.empty()
            finally:
                stream.close_clean()
                await bus.stop()
        assert transport.event_connect_count == 1

    asyncio.run(go())


def test_run_loop_reconnects_on_drop_and_emits_sentinel() -> None:
    """Upstream drop: bus reconnects, broadcasts _BusReconnected sentinel."""

    async def go() -> None:
        transport = _BusTransport()
        first = transport.queue_stream()
        second = transport.queue_stream()
        async with _async_client_with(transport) as client:
            # Tight reconnect interval so the test is fast.
            bus = _OpenCodeEventBus(
                client,
                reconnect_initial=0.05,
                reconnect_max=0.05,
                connect_timeout=2.0,
            )
            await bus.start()
            try:
                async with bus.subscribe("ses_x") as q:
                    first.emit(_evt("session.idle", "ses_x"))
                    e1 = await asyncio.wait_for(q.get(), timeout=2.0)
                    assert e1["type"] == "session.idle"
                    # Drop the connection.
                    first.close_with_exception(RuntimeError("connection lost"))
                    # Bus reconnects → broadcasts _BusReconnected.
                    sentinel = await asyncio.wait_for(q.get(), timeout=2.0)
                    assert isinstance(sentinel, _BusReconnected)
                    # Second connection now active — events flow again.
                    second.emit(_evt("session.idle", "ses_x"))
                    e2 = await asyncio.wait_for(q.get(), timeout=2.0)
                    assert e2["type"] == "session.idle"
            finally:
                second.close_clean()
                await bus.stop()
        assert transport.event_connect_count >= 2
        assert bus._reconnect_count >= 1  # noqa: SLF001

    asyncio.run(go())


def test_aclose_stops_bus() -> None:
    """OpenCodeClient.aclose() stops the bus before closing the transport."""

    async def go() -> None:
        transport = _BusTransport()
        stream = transport.queue_stream()
        client = OpenCodeClient(
            "http://test-opencode",
            timeout=1.0,
        )
        # Replace the auto-built client with our scripted one.
        await client._client.aclose()  # noqa: SLF001
        client._client = _async_client_with(transport)  # noqa: SLF001

        await client.start_event_bus(connect_timeout=2.0)
        bus = client.event_bus
        assert bus is not None
        # Let the run loop reach connected state.
        await asyncio.wait_for(bus._first_connected.wait(), timeout=2.0)  # noqa: SLF001
        assert bus._task is not None  # noqa: SLF001
        stream.close_clean()
        await client.aclose()
        assert bus._task is None  # noqa: SLF001

    asyncio.run(go())


def test_start_event_bus_idempotent_on_client() -> None:
    """OpenCodeClient.start_event_bus() can be called twice."""

    async def go() -> None:
        transport = _BusTransport()
        stream = transport.queue_stream()
        client = OpenCodeClient("http://test-opencode")
        await client._client.aclose()  # noqa: SLF001
        client._client = _async_client_with(transport)  # noqa: SLF001

        bus_a = await client.start_event_bus(connect_timeout=2.0)
        bus_b = await client.start_event_bus(connect_timeout=2.0)
        assert bus_a is bus_b
        stream.close_clean()
        await client.aclose()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# chat_stream via bus — concurrency acceptance criterion
# ---------------------------------------------------------------------------


def _emit_basic_turn(stream: _ScriptedSSEStream, session_id: str, text: str) -> None:
    """Push a minimal valid turn: one delta + session.idle."""
    stream.emit(
        _evt(
            "message.part.delta",
            session_id,
            messageID=f"msg_{session_id}",
            partID=f"prt_{session_id}",
            field="text",
            delta=text,
        )
    )
    stream.emit(_evt("session.idle", session_id))


def test_chat_stream_via_bus_yields_events_and_terminates_on_idle() -> None:
    """A single chat_stream turn over the bus terminates on session.idle."""

    async def go() -> None:
        transport = _BusTransport()
        stream = transport.queue_stream()
        client = OpenCodeClient("http://test-opencode")
        await client._client.aclose()  # noqa: SLF001
        client._client = _async_client_with(transport)  # noqa: SLF001

        await client.start_event_bus(connect_timeout=2.0)

        # Schedule the events to arrive after we've subscribed.
        async def _push_after_subscribe() -> None:
            await asyncio.sleep(0.05)
            _emit_basic_turn(stream, "ses_one", "hello")

        push_task = asyncio.create_task(_push_after_subscribe())

        events: List[Dict[str, Any]] = []
        async for event in client.chat_stream("ses_one", "hi there"):
            events.append(event)

        await push_task
        # We got the delta + idle.
        assert any(e["type"] == "message.part.delta" for e in events), (
            "delta event missing"
        )
        assert events[-1]["type"] == "session.idle"
        # POST landed on the right path.
        assert any(
            c["path"] == "/session/ses_one/message" for c in transport.post_calls
        )
        stream.close_clean()
        await client.aclose()
        # Bus opened exactly ONE upstream /event connection.
        assert transport.event_connect_count == 1

    asyncio.run(go())


def test_chat_stream_concurrent_share_one_upstream_connection() -> None:
    """K parallel chat_stream calls share the single bus connection.

    This is the headline acceptance criterion for task 105013: no
    matter how many concurrent turns are in flight, the bus only ever
    holds one upstream ``GET /event`` connection.
    """

    async def go() -> None:
        transport = _BusTransport()
        stream = transport.queue_stream()
        client = OpenCodeClient("http://test-opencode")
        await client._client.aclose()  # noqa: SLF001
        client._client = _async_client_with(transport)  # noqa: SLF001

        await client.start_event_bus(connect_timeout=2.0)

        K = 8
        session_ids = [f"ses_{i}" for i in range(K)]

        async def _consume(session_id: str) -> List[str]:
            kinds: List[str] = []
            async for event in client.chat_stream(session_id, "hi"):
                kinds.append(event["type"])
            return kinds

        async def _pump_events() -> None:
            # Wait until POSTs have landed → subscribers are on the bus.
            for _ in range(50):
                if len(transport.post_calls) == K:
                    break
                await asyncio.sleep(0.02)
            for sid in session_ids:
                _emit_basic_turn(stream, sid, f"text-{sid}")

        consumers = [asyncio.create_task(_consume(sid)) for sid in session_ids]
        pumper = asyncio.create_task(_pump_events())

        results = await asyncio.wait_for(asyncio.gather(*consumers), timeout=5.0)
        await pumper

        # All K turns terminated cleanly on session.idle.
        for kinds in results:
            assert kinds[-1] == "session.idle", kinds
            assert "message.part.delta" in kinds, kinds
        # And throughout, exactly ONE /event connection.
        assert transport.event_connect_count == 1
        # K POSTs hit the message endpoint (one per session).
        message_posts = [
            c for c in transport.post_calls if c["path"].endswith("/message")
        ]
        assert len(message_posts) == K

        stream.close_clean()
        await client.aclose()

    asyncio.run(go())


def test_chat_stream_via_bus_raises_on_reconnect_mid_turn() -> None:
    """A bus reconnect during a turn surfaces as OpenCodeSessionError."""

    async def go() -> None:
        transport = _BusTransport()
        first = transport.queue_stream()
        second = transport.queue_stream()
        client = OpenCodeClient("http://test-opencode")
        await client._client.aclose()  # noqa: SLF001
        client._client = _async_client_with(transport)  # noqa: SLF001

        await client.start_event_bus(
            reconnect_initial=0.05,
            reconnect_max=0.05,
            connect_timeout=2.0,
        )

        async def _push_then_drop() -> None:
            # Wait for the POST to land.
            for _ in range(50):
                if transport.post_calls:
                    break
                await asyncio.sleep(0.02)
            # Push one event then drop the upstream — the bus
            # reconnects and broadcasts _BusReconnected.
            first.emit(
                _evt(
                    "message.part.delta",
                    "ses_drop",
                    field="text",
                    delta="partial",
                )
            )
            await asyncio.sleep(0.05)
            first.close_with_exception(RuntimeError("network hiccup"))

        pump = asyncio.create_task(_push_then_drop())

        with pytest.raises(OpenCodeSessionError, match="reconnected mid-turn"):
            async for _ in client.chat_stream("ses_drop", "hi"):
                pass

        await pump
        second.close_clean()
        await client.aclose()

    asyncio.run(go())


def test_chat_stream_legacy_path_unchanged_when_bus_not_started() -> None:
    """Without start_event_bus(), chat_stream uses the per-request path.

    The legacy path opens its own GET /event connection per turn —
    here we just verify it doesn't hit the bus assertion and that
    the connection count grows per turn.
    """

    async def go() -> None:
        transport = _BusTransport()
        first = transport.queue_stream()
        second = transport.queue_stream()
        client = OpenCodeClient("http://test-opencode")
        await client._client.aclose()  # noqa: SLF001
        client._client = _async_client_with(transport)  # noqa: SLF001

        # Do NOT call start_event_bus.
        assert client.event_bus is None

        async def _run_one(stream: _ScriptedSSEStream, sid: str) -> None:
            async def _push() -> None:
                # Wait for POST to land before emitting.
                for _ in range(50):
                    if any(
                        c["path"].endswith(f"/{sid}/message")
                        for c in transport.post_calls
                    ):
                        break
                    await asyncio.sleep(0.02)
                _emit_basic_turn(stream, sid, "ok")

            pump = asyncio.create_task(_push())
            kinds: List[str] = []
            async for event in client.chat_stream(sid, "hi"):
                kinds.append(event["type"])
            await pump
            stream.close_clean()
            assert kinds[-1] == "session.idle"

        await _run_one(first, "ses_a")
        await _run_one(second, "ses_b")

        # Two turns → two upstream /event connections (legacy
        # per-request behaviour).
        assert transport.event_connect_count == 2

        await client.aclose()

    asyncio.run(go())

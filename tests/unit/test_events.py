"""Tests for :mod:`scufris_server.events` (steps 2 + 5 of #11).

Covers:

- :class:`ThinkingEvent` dataclass wire-shape contract (step 2).
- :class:`EventBus` lifecycle, subscription, dispatch, and broadcast
  surface (step 5). Reader-loop integration with the live opencode
  ``/event`` endpoint is exercised in
  ``tests/integration/test_chat_stream_real.py`` (step 11) — the
  unit tests here patch ``_run`` to a controllable async noop and
  poke ``_dispatch`` / ``_broadcast`` directly because driving the
  real reader loop with a respx-mocked streaming response is
  brittle and doesn't add coverage the integration test won't.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from scufris_server.events import SCUFRIS_SOURCE, EventBus, ThinkingEvent
from scufris_server.opencode_client import OpencodeUnavailable, _BusReconnected

# ---------------------------------------------------------------------------
# Constant
# ---------------------------------------------------------------------------


def test_scufris_source_is_the_post_swap_label() -> None:
    """v2 surfaces a single agent identity per ADR-10's mapping policy.

    The v1 thinking-trail renderer reads :attr:`ThinkingEvent.source`
    to attribute events; v2 collapses every event to ``"scufris"``
    because opencode owns the agent hierarchy below the bus.
    """
    assert SCUFRIS_SOURCE == "scufris"


# ---------------------------------------------------------------------------
# ThinkingEvent — required-only construction
# ---------------------------------------------------------------------------


def test_thinking_event_minimum_fields_round_trip() -> None:
    """A ``text`` event with only required fields serialises to exactly
    those four keys; no optional fields leak onto the wire.
    """
    ev = ThinkingEvent(
        kind="text",
        source=SCUFRIS_SOURCE,
        text="hello",
        depth=0,
    )
    assert ev.to_payload() == {
        "kind": "text",
        "source": "scufris",
        "text": "hello",
        "depth": 0,
    }


def test_thinking_event_defaults_are_all_none() -> None:
    """Every optional field defaults to ``None``. Guards against a
    future refactor that swaps a default to ``0`` or ``""`` and
    silently changes the wire shape (None is filtered; 0 / "" are
    not)."""
    ev = ThinkingEvent(
        kind="text",
        source=SCUFRIS_SOURCE,
        text="x",
        depth=0,
    )
    assert ev.arg is None
    assert ev.context is None
    assert ev.prior_turns is None
    assert ev.evicted is None
    assert ev.new_facts is None


# ---------------------------------------------------------------------------
# to_payload() field filtering
# ---------------------------------------------------------------------------


def test_to_payload_includes_only_non_none_optionals() -> None:
    """Mixed optional fields: ``arg`` set, others unset. Only the
    populated optional surfaces; the unset ones stay off the wire so
    v1 clients reading ``payload.get(...)`` see clean shape.
    """
    ev = ThinkingEvent(
        kind="tool_call",
        source=SCUFRIS_SOURCE,
        text="bash",
        depth=0,
        arg="ls -la",
    )
    payload = ev.to_payload()
    assert payload == {
        "kind": "tool_call",
        "source": "scufris",
        "text": "bash",
        "depth": 0,
        "arg": "ls -la",
    }
    assert "context" not in payload
    assert "prior_turns" not in payload


def test_to_payload_surfaces_every_optional_when_all_set() -> None:
    """Verifies the field-introspection serialiser walks all five
    optional fields, not a hard-coded subset. If a new optional
    field is added to the dataclass it gets serialised
    automatically.
    """
    ev = ThinkingEvent(
        kind="compaction",
        source=SCUFRIS_SOURCE,
        text="salvaged 3 messages",
        depth=0,
        arg="auto",
        context="user said remember",
        prior_turns=2,
        evicted=3,
        new_facts=1,
    )
    payload = ev.to_payload()
    assert payload == {
        "kind": "compaction",
        "source": "scufris",
        "text": "salvaged 3 messages",
        "depth": 0,
        "arg": "auto",
        "context": "user said remember",
        "prior_turns": 2,
        "evicted": 3,
        "new_facts": 1,
    }


def test_to_payload_keeps_zero_and_empty_string_when_explicitly_set() -> None:
    """``0`` and ``""`` are valid distinct-from-None values and must
    not be filtered. Guards against a sloppy ``if value`` instead of
    ``if value is not None`` in the serialiser.
    """
    ev = ThinkingEvent(
        kind="tool_call",
        source=SCUFRIS_SOURCE,
        text="echo",
        depth=0,
        arg="",  # empty string — legitimate "no arg" signal
        prior_turns=0,  # zero — legitimate count
    )
    payload = ev.to_payload()
    assert payload["arg"] == ""
    assert payload["prior_turns"] == 0


# ---------------------------------------------------------------------------
# Wire-shape stability (v1 compatibility)
# ---------------------------------------------------------------------------


def test_to_payload_field_order_is_canonical_required_first() -> None:
    """Required fields come before optionals in the serialised dict.

    Dict ordering is insertion-ordered in Python 3.7+; we rely on
    that for deterministic JSON output. Useful for diffing SSE
    traces in tests and logs.
    """
    ev = ThinkingEvent(
        kind="tool_result",
        source=SCUFRIS_SOURCE,
        text="bash",
        depth=0,
        arg="ok",
    )
    keys = list(ev.to_payload().keys())
    assert keys[:4] == ["kind", "source", "text", "depth"]
    assert keys[4] == "arg"


def test_thinking_event_kinds_cover_v1_spectrum() -> None:
    """All five v1 kinds are accepted (typing-Literal would catch
    drift at static-analysis time; this test guards runtime
    construction). If we ever add a sixth kind, this list expands.
    """
    for kind in ("text", "tool_call", "tool_result", "tool_meta", "compaction"):
        ev = ThinkingEvent(
            kind=kind,  # type: ignore[arg-type]  # Literal narrowing across loop
            source=SCUFRIS_SOURCE,
            text="x",
            depth=0,
        )
        assert ev.to_payload()["kind"] == kind


# ---------------------------------------------------------------------------
# EventBus — construction & initial state
# ---------------------------------------------------------------------------


async def _make_bus(
    *,
    connect_timeout: float = 30.0,
    reconnect_initial: float = 1.0,
    reconnect_max: float = 30.0,
) -> EventBus:
    """Construct a bus over an unused ``httpx.AsyncClient`` instance.

    All EventBus unit tests below either bypass the reader loop
    entirely (by monkeypatching ``_run``) or poke
    :meth:`_dispatch` / :meth:`_broadcast` directly — none of them
    actually call ``self._client.stream("GET", "/event")``, so the
    AsyncClient instance is never used for HTTP. Held only because
    :class:`EventBus.__init__` requires one.
    """
    return EventBus(
        httpx.AsyncClient(base_url="http://opencode.test"),
        connect_timeout=connect_timeout,
        reconnect_initial=reconnect_initial,
        reconnect_max=reconnect_max,
    )


@pytest.mark.asyncio
async def test_event_bus_init_state() -> None:
    """Fresh bus is not connected, no subscribers, no reconnects.

    Locks the contract that ``connected`` is ``False`` *before*
    :meth:`start` and that ``stats`` returns a sane initial
    snapshot (so a startup ``/v1/stats`` doesn't crash on an
    unstarted bus).
    """
    bus = await _make_bus()
    assert bus.connected is False
    assert bus.stats == {
        "connected": False,
        "reconnects": 0,
        "subscribers": 0,
        "sessions": 0,
        "dropped_events": 0,
        "max_queue_depth": 0,
    }


# ---------------------------------------------------------------------------
# EventBus — lifecycle (start / stop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_bus_start_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling :meth:`start` twice spawns only one reader task.

    Critical for the FastAPI lifespan: if a future refactor adds a
    second ``await bus.start()`` (e.g. on hot reload), we must not
    end up with two competing reader loops sharing one
    ``AsyncClient``. Patches ``_run`` to a tiny await so the test
    doesn't open a real HTTP connection.
    """
    bus = await _make_bus()

    async def _noop() -> None:
        # Wait until stop signals us — mirrors _run's exit contract.
        await bus._stop_event.wait()

    monkeypatch.setattr(bus, "_run", _noop)
    try:
        await bus.start()
        task1 = bus._task
        await bus.start()
        task2 = bus._task
        assert task1 is task2
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_event_bus_stop_without_start_is_a_noop() -> None:
    """:meth:`stop` on an unstarted bus does nothing — important for
    error paths in the lifespan (if startup fails before
    ``bus.start()`` runs, the lifespan still calls ``bus.stop()``
    in its cleanup, and we must not raise).
    """
    bus = await _make_bus()
    await bus.stop()  # no exception
    assert bus._task is None
    assert bus.connected is False


@pytest.mark.asyncio
async def test_event_bus_stop_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling :meth:`stop` twice is fine. Guards against a partial
    shutdown path that retries stop after a cancellation.
    """
    bus = await _make_bus()

    async def _noop() -> None:
        await bus._stop_event.wait()

    monkeypatch.setattr(bus, "_run", _noop)
    await bus.start()
    await bus.stop()
    await bus.stop()  # second call must be a no-op
    assert bus._task is None


# ---------------------------------------------------------------------------
# EventBus — subscribe()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_bus_subscribe_times_out_when_not_connected() -> None:
    """If the bus never connects, :meth:`subscribe` raises
    :class:`OpencodeUnavailable` after the per-call timeout.

    Mirrors v1's policy: an unreachable opencode at chat-stream
    open time is the same class of failure as
    :meth:`OpencodeClient.health` not responding — caller can
    decide to surface 503 or fall back. Short timeout (50 ms) so
    the test stays fast.
    """
    bus = await _make_bus(connect_timeout=0.05)
    with pytest.raises(OpencodeUnavailable, match="not connected within"):
        async with bus.subscribe("ses_x"):
            pass  # pragma: no cover — never enters block


@pytest.mark.asyncio
async def test_event_bus_subscribe_registers_and_unregisters_queue() -> None:
    """Inside the ``async with`` block, a queue is registered under
    ``session_id``. After exit, the entry is removed (no leak in
    ``_subs`` across turns). Bypasses the connect-wait by setting
    ``_first_connected`` manually.
    """
    bus = await _make_bus()
    bus._first_connected.set()  # short-circuit the wait

    assert bus._subs == {}
    async with bus.subscribe("ses_x") as q:
        assert isinstance(q, asyncio.Queue)
        assert "ses_x" in bus._subs
        assert len(bus._subs["ses_x"]) == 1
    # Block exited — subs cleaned up.
    assert "ses_x" not in bus._subs
    assert bus._subs == {}


# ---------------------------------------------------------------------------
# EventBus — _dispatch() (synchronous, no I/O)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_bus_dispatch_routes_event_to_matching_subscriber() -> None:
    """Event with matching ``properties.sessionID`` lands on the
    subscriber's queue.
    """
    bus = await _make_bus()
    bus._first_connected.set()
    async with bus.subscribe("ses_x") as q:
        event: dict[str, Any] = {
            "type": "message.part.delta",
            "properties": {"sessionID": "ses_x", "text": "hi"},
        }
        bus._dispatch(event)
        assert q.qsize() == 1
        assert q.get_nowait() is event


@pytest.mark.asyncio
async def test_event_bus_dispatch_drops_event_without_session_id() -> None:
    """Global events (``server.connected``, ``lsp.*``,
    ``installation.*``) carry no sessionID and are dropped at
    dispatch — :mod:`scufris_server.event_mapping` ignores them
    anyway, so dropping at the bus saves queue churn.
    """
    bus = await _make_bus()
    bus._first_connected.set()
    async with bus.subscribe("ses_x") as q:
        bus._dispatch({"type": "server.connected"})  # no properties
        bus._dispatch({"type": "lsp.diagnostic", "properties": {}})  # no sessionID
        bus._dispatch(
            {"type": "x", "properties": {"sessionID": 123}}  # not a string
        )
        assert q.qsize() == 0


@pytest.mark.asyncio
async def test_event_bus_dispatch_drops_event_for_unknown_session() -> None:
    """Event for a session with no live subscriber is silently
    dropped (don't accumulate in some "pending" buffer — opencode
    re-emits state on session-state queries if needed).
    """
    bus = await _make_bus()
    bus._first_connected.set()
    async with bus.subscribe("ses_x") as q:
        bus._dispatch({"type": "x", "properties": {"sessionID": "ses_other"}})
        assert q.qsize() == 0


@pytest.mark.asyncio
async def test_event_bus_dispatch_fans_out_to_multiple_subscribers() -> None:
    """Two subscribers on the same session both receive the event.

    Edge case: doesn't happen in normal scufris usage (each chat
    turn opens one queue) but the data structure supports it
    (``_subs[sid]`` is a list), so we lock the contract.
    """
    bus = await _make_bus()
    bus._first_connected.set()
    async with bus.subscribe("ses_x") as q1, bus.subscribe("ses_x") as q2:
        event = {"type": "x", "properties": {"sessionID": "ses_x"}}
        bus._dispatch(event)
        assert q1.qsize() == 1
        assert q2.qsize() == 1
        assert q1.get_nowait() is event
        assert q2.get_nowait() is event


# ---------------------------------------------------------------------------
# EventBus — _broadcast() (the _BusReconnected fan-out path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_bus_broadcast_reaches_every_subscriber() -> None:
    """A :class:`_BusReconnected` sentinel via :meth:`_broadcast`
    lands on every live queue regardless of session id. This is
    how a reconnect storm signals all in-flight turns that they
    may have missed events.
    """
    bus = await _make_bus()
    bus._first_connected.set()
    async with (
        bus.subscribe("ses_a") as qa,
        bus.subscribe("ses_b") as qb,
    ):
        sentinel = _BusReconnected()
        await bus._broadcast(sentinel)
        assert qa.get_nowait() is sentinel
        assert qb.get_nowait() is sentinel

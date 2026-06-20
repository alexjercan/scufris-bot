"""Server-side opencode event consumer + the wire shape it emits.

Owns the **bridge** between opencode's global ``GET /event`` SSE
stream and the per-session events ``POST /v1/chat/stream`` fans out
to clients (#11 — ``tasks/20260613-091045``: "SSE streaming of
opencode 'thinking' events"). The module contains two layers:

1. :class:`ThinkingEvent` — the user-visible event dataclass.
   Verbatim port of v1's
   ``feature/opencode:utils/callbacks.py:128-155`` so the v1 SSE
   wire format (and the existing CLI / Telegram renderers that read
   it) keep working unchanged against the v2 server. Pure data —
   no I/O, no opencode-specific knowledge.
2. :class:`EventBus` — the ONE persistent consumer of opencode's
   ``GET /event`` stream per scufris-server process (ADR-10).
   Holds a single shared connection and fans events out to
   per-session asyncio queues subscribed via async context
   manager. Verbatim port of v1's
   ``feature/opencode:utils/opencode_client.py:130-380``
   (``_OpenCodeEventBus``) — leading underscore dropped because
   v2 treats it as public API.

Mapping from raw opencode events to :class:`ThinkingEvent` lives in
:mod:`scufris_server.event_mapping` (step 3). Outbound SSE framing
lives in :mod:`scufris_server.sse` (step 6). The route handler
``POST /v1/chat/stream`` (step 8) glues them together.

Why three modules instead of a package? Total LoC across the trio
is comfortably under ``sessions.py`` (~302 lines); a package would
add namespace overhead without proportional clarity gain. Revisit
if #28 (observability) or #30 (permissions) materially grow this
file past ~500 LoC — the placeholder docstring already flagged
``scufris_server/events/`` as the escape hatch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, fields
from typing import Any, Literal

import httpx

from scufris_server.opencode_client import OpencodeUnavailable, _BusReconnected

logger = logging.getLogger("scufris-server.events")


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: The single ``source`` value every :class:`ThinkingEvent` carries
#: post-OpenCode-swap. v1's runtime distinguished ``"main"`` from
#: sub-agent identifiers (e.g. ``"knowledge_agent"``); the field is
#: kept on the dataclass for wire compatibility with renderers that
#: still read it, but every event scufris-server emits gets this
#: single constant. Sub-agent visibility now lives in the
#: ``tool_call`` ``text`` field (the agent-tool's name) instead.
SCUFRIS_SOURCE: str = "scufris"


# ---------------------------------------------------------------------------
# Wire dataclass
# ---------------------------------------------------------------------------


@dataclass
class ThinkingEvent:
    """A user-visible "thinking" event surfaced into the SSE trail.

    Verbatim port of v1's ``utils.callbacks.ThinkingEvent`` so the
    existing CLI / Telegram thinking-trail renderers keep working
    unchanged against the v2 server. Each instance becomes one
    ``event: thinking`` SSE chunk via :meth:`to_payload` plus
    :func:`scufris_server.sse.format_event`.

    Fields
    ------
    kind:
        Coarse event class. ``text`` carries a streamed model
        token chunk; ``tool_call`` announces a tool invocation;
        ``tool_result`` reports its outcome; ``tool_meta`` carries
        side-band annotations (permission asks, sub-agent
        history-load counts); ``compaction`` reports a history
        salvage in progress.
    source:
        Origin label. Always :data:`SCUFRIS_SOURCE` in v2 — v1's
        sub-agent identifiers don't apply since opencode owns the
        agent hierarchy. Kept as a field so the v1 wire format
        stays binary-compatible.
    text:
        Free-form content. Semantics depend on ``kind``: for
        ``text`` it's the streamed token; for ``tool_call`` /
        ``tool_result`` it's the tool's technical name (the
        renderer maps this to a display name); for ``tool_meta``
        it's the annotation message.
    depth:
        Nesting level for indentation / styling. Always ``0`` in
        v2 (single agent post-swap), but kept on the dataclass for
        wire compatibility.
    arg:
        Optional human-meaningful argument for ``tool_call``. The
        mapping layer (:mod:`scufris_server.event_mapping`)
        extracts a short summary from opencode's tool state — the
        ``state.title`` if populated, else a best-effort one-liner
        from ``state.input``. Capped at ~120 chars upstream.
    context:
        Phase-2 sub-agent ``context`` field. Always ``None`` in v2
        — kept for v1 wire compatibility; the mapping layer never
        populates it.
    prior_turns:
        For ``tool_meta`` events emitted in v1's ``on_tool_end``,
        the count of prior-turn history a sub-agent loaded. v2
        does not populate this (no sub-agent hierarchy); field
        kept for v1 compatibility only.
    evicted:
        For ``compaction`` events: count of messages dropped by
        the history salvage. v2 does not populate this directly
        — opencode owns compaction (design §9.5); the field is
        reserved for the plugin to relay if a hook ever surfaces
        the count.
    new_facts:
        Sibling to :attr:`evicted` — count of facts extracted
        during a salvage. Same v2-reserved status.

    Serialisation
    -------------
    :meth:`to_payload` returns a compact dict suitable for JSON
    encoding into the SSE ``data:`` line. Only non-None optional
    fields are surfaced so the wire stays small and v1 clients
    (which use ``payload.get(...)`` everywhere) are unaffected by
    additional unset keys.
    """

    kind: Literal["text", "tool_call", "tool_result", "tool_meta", "compaction"]
    source: str  # e.g. "scufris" (single-agent post-swap)
    text: str  # for tool_call: target tool name; for text: the message
    depth: int  # nesting level (for indentation/styling) — always 0 in v2
    arg: str | None = None  # human-meaningful argument, if any
    context: str | None = None  # Phase-2 sub-agent `context` field, if any
    # Phase 3.5 — for `tool_meta` events emitted in `on_tool_end`,
    # the count of prior history turns the sub-agent loaded for THIS
    # call (>0 only). Always None in v2.
    prior_turns: int | None = None
    # Phase 3 — for `compaction` events: how many messages were
    # evicted in the salvage and how many new facts were extracted.
    # Both >0 (history manager only emits when something was actually
    # salvaged). Reserved in v2.
    evicted: int | None = None
    new_facts: int | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON-serialisable form for the SSE ``data:`` line.

        Required fields (``kind``, ``source``, ``text``, ``depth``)
        always present; optional fields (``arg``, ``context``,
        ``prior_turns``, ``evicted``, ``new_facts``) are included
        only when non-None. Keeps the wire compact and matches v1's
        ``utils.callbacks.ThinkingEvent``-to-JSON shape (v1
        serialised via ``dataclasses.asdict`` + ``{k: v for k, v in
        ... if v is not None}``; we do it inline to avoid the asdict
        round-trip).
        """
        payload: dict[str, Any] = {
            "kind": self.kind,
            "source": self.source,
            "text": self.text,
            "depth": self.depth,
        }
        # Iterate only the optional fields. Fields are introspected
        # rather than hard-coded so adding a new optional field
        # (e.g. for a future phase) doesn't drift the serialiser.
        for field_def in fields(self):
            if field_def.name in ("kind", "source", "text", "depth"):
                continue
            value = getattr(self, field_def.name)
            if value is not None:
                payload[field_def.name] = value
        return payload


# ---------------------------------------------------------------------------
# EventBus — single shared /event connection with per-session fan-out
# ---------------------------------------------------------------------------


class EventBus:
    """Single shared ``GET /event`` connection with per-session fan-out.

    Why: ``opencode serve``'s ``/event`` bus is server-global —
    every connection gets every event for every session — so N
    concurrent chat turns opening N parallel SSE connections is
    just N copies of the same stream. Owning one persistent
    connection in the FastAPI lifespan and dispatching events to
    per-session queues collapses that down to 1 (ADR-10).

    Lifecycle
    ---------
    - :meth:`start` spawns the background reader task. Idempotent.
    - :meth:`stop` cancels the reader and waits for it. Idempotent.
    - :meth:`subscribe` (async context manager) registers a queue
      for a given ``session_id`` and removes it on exit.

    The reader auto-reconnects with exponential backoff when the
    upstream connection drops. On every reconnect (after the first
    successful connect) a
    :class:`scufris_server.opencode_client._BusReconnected` sentinel
    is broadcast to every live subscriber so consumers can choose
    to error out or refetch state. The streaming chat handler
    (:mod:`scufris_server.routes.chat_stream`, step 8 of #11)
    surfaces this as an ``error`` SSE event per the consumer policy
    documented on the sentinel itself.

    The bus drops events whose ``properties.sessionID`` is missing
    or has no matching subscriber — global events
    (``server.connected``, ``installation.*``, ``lsp.*``) carry no
    sessionID and the :mod:`scufris_server.event_mapping` layer
    ignores them anyway, so dropping them at the dispatch layer
    is loss-free.

    Verbatim port of v1's
    ``feature/opencode:utils/opencode_client.py:130-380``
    (``_OpenCodeEventBus``) — modernised typing (PEP 604 unions,
    lowercase generics) and re-pointed at scufris-server's
    exception hierarchy: subscribe-timeout raises
    :class:`OpencodeUnavailable` (was v1's
    ``OpenCodeSessionError``) since the semantic is "upstream not
    reachable", matching :meth:`OpencodeClient.health`'s collapse
    policy. Internal-only raises inside the reader loop are caught
    by :meth:`_run` before they escape, so they need no specific
    type.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 30.0,
        connect_timeout: float = 30.0,
        logger_: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            client: Shared :class:`httpx.AsyncClient` (owned by the
                outer :class:`OpencodeClient`, borrowed via its
                ``httpx_client`` property). The bus uses it but
                does NOT close it.
            reconnect_initial: First backoff delay, seconds.
            reconnect_max: Cap on the backoff delay, seconds.
            connect_timeout: How long :meth:`subscribe` waits for
                the bus to reach ``connected=True`` for the first
                time before raising :class:`OpencodeUnavailable`.
                Once the bus has connected at least once, subscribe
                is instantaneous (subsequent disconnects are
                surfaced via the
                :class:`_BusReconnected` sentinel mid-stream).
            logger_: Optional logger override (defaults to the
                module logger).
        """
        self._client = client
        self._reconnect_initial = reconnect_initial
        self._reconnect_max = reconnect_max
        self._connect_timeout = connect_timeout
        self._subs: dict[str, list[asyncio.Queue[Any]]] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._first_connected = asyncio.Event()
        self._connected = False
        self._reconnect_count = 0
        self._dropped_count = 0
        self._logger = logger_ or logger

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the background reader task. Idempotent."""
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="scufris-event-bus")

    async def stop(self) -> None:
        """Cancel the reader and wait for it. Idempotent."""
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None
        self._connected = False

    @property
    def connected(self) -> bool:
        """``True`` while the upstream stream is currently open."""
        return self._connected

    @property
    def stats(self) -> dict[str, Any]:
        """Snapshot of bus health metrics for ``/v1/stats`` etc.

        Reads all counters atomically-enough (single-threaded
        asyncio loop) — no extra locking. ``max_queue_depth`` is
        the *current* max across live queues, not a rolling max.
        """
        max_depth = 0
        sub_total = 0
        for qs in self._subs.values():
            for q in qs:
                sub_total += 1
                if q.qsize() > max_depth:
                    max_depth = q.qsize()
        return {
            "connected": self._connected,
            "reconnects": self._reconnect_count,
            "subscribers": sub_total,
            "sessions": len(self._subs),
            "dropped_events": self._dropped_count,
            "max_queue_depth": max_depth,
        }

    # ------------------------------------------------------------------
    # Subscription API
    # ------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def subscribe(
        self,
        session_id: str,
        *,
        connect_timeout: float | None = None,
    ) -> AsyncIterator[asyncio.Queue[Any]]:
        """Register a queue for ``session_id`` for the duration of the block.

        Yields an :class:`asyncio.Queue` onto which the bus drops
        events whose ``properties.sessionID`` matches
        ``session_id``. Subscribers may also put their own sentinels
        onto the queue — the bus only emits :class:`dict` events
        plus :class:`_BusReconnected` and never reads from the
        queue.

        Args:
            session_id: opencode session id to receive events for.
            connect_timeout: Override for the bus's default
                first-connect timeout. ``None`` uses the bus
                default.
        """
        queue: asyncio.Queue[Any] = asyncio.Queue()
        async with self._lock:
            self._subs.setdefault(session_id, []).append(queue)
        try:
            timeout = (
                connect_timeout
                if connect_timeout is not None
                else self._connect_timeout
            )
            try:
                await asyncio.wait_for(self._first_connected.wait(), timeout=timeout)
            except TimeoutError as exc:
                raise OpencodeUnavailable(
                    f"event bus not connected within {timeout:.1f}s"
                ) from exc
            yield queue
        finally:
            async with self._lock:
                queues = self._subs.get(session_id)
                if queues is not None:
                    try:
                        queues.remove(queue)
                    except ValueError:
                        pass
                    if not queues:
                        self._subs.pop(session_id, None)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """Reader loop with exponential-backoff reconnect."""
        backoff = self._reconnect_initial
        first = True
        while not self._stop_event.is_set():
            try:
                await self._read_once(emit_reconnect=not first)
                # Stream ended cleanly (server closed). Loop and reconnect.
                self._logger.info(
                    "scufris event bus: upstream stream closed cleanly; will reconnect"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — must keep loop alive
                self._logger.warning(
                    "scufris event bus: upstream error (%s); reconnect in %.1fs",
                    exc,
                    backoff,
                )
            self._connected = False
            first = False
            self._reconnect_count += 1
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                # Stop signalled during backoff.
                return
            except TimeoutError:
                pass
            backoff = min(backoff * 2, self._reconnect_max)

    async def _read_once(self, *, emit_reconnect: bool) -> None:
        """Open one upstream connection and dispatch until it closes."""
        async with self._client.stream("GET", "/event", timeout=None) as resp:
            if resp.status_code >= 400:
                text = await resp.aread()
                raise RuntimeError(
                    f"GET /event: {resp.status_code} "
                    f"{text.decode('utf-8', errors='replace')[:200]}"
                )
            self._connected = True
            self._first_connected.set()
            if emit_reconnect:
                await self._broadcast(_BusReconnected())
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    self._logger.warning(
                        "scufris event bus: dropping malformed SSE line"
                    )
                    continue
                if not isinstance(event, dict):
                    continue
                self._dispatch(event)

    def _dispatch(self, event: dict[str, Any]) -> None:
        """Route one event to subscribers keyed by ``properties.sessionID``."""
        props = event.get("properties") or {}
        sid = props.get("sessionID")
        if not isinstance(sid, str):
            return  # global event (server.connected, lsp.*, …) — no consumer
        queues = self._subs.get(sid)
        if not queues:
            return
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover — queues unbounded
                self._dropped_count += 1

    async def _broadcast(self, item: Any) -> None:
        """Push one sentinel onto every live subscriber queue."""
        async with self._lock:
            queues = [q for qs in self._subs.values() for q in qs]
        for q in queues:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:  # pragma: no cover
                self._dropped_count += 1

"""HTTP client for the OpenCode local daemon (``opencode serve``).

A thin async wrapper around the OpenCode REST + SSE API tuned for
Scufris's needs:

- One :class:`httpx.AsyncClient` per :class:`OpenCodeClient`. Lifecycle
  is owned by the caller — :func:`scufris_server.bootstrap.build_runtime`
  constructs an instance; the FastAPI lifespan calls :meth:`aclose` on
  shutdown.
- Bypasses the ``opencode_ai`` SDK for event parsing because the SDK's
  pydantic types omit several event types and ``Session`` fields the
  live server emits (see ``tasks/20260610-101413/SCHEMA.md``).
- :meth:`chat_stream` opens ``GET /event`` *before* issuing
  ``POST /session/{id}/message`` and yields raw event dicts filtered by
  ``properties.sessionID``. Termination is driven by ``session.idle``
  (success) or ``session.error`` (failure) for the in-flight session.
- Optional :class:`_OpenCodeEventBus` (started via
  :meth:`OpenCodeClient.start_event_bus`) holds a single shared
  ``GET /event`` connection for the whole process and fans events
  out to per-session asyncio queues. When started, :meth:`chat_stream`
  subscribes through the bus instead of opening its own connection,
  which keeps the upstream connection count at 1 regardless of how
  many concurrent chat turns are in flight (task
  ``tasks/20260610-105013``). The legacy per-request path stays in
  place for test stubs and back-compat with callers that don't run
  the bus.

Exception hierarchy:

- :class:`OpenCodeError` — base, never raised directly.
- :class:`OpenCodeSessionError` — non-stale REST/stream failure (5xx,
  network error, malformed JSON, ``session.error`` event for our
  session, or stream closed before ``session.idle``).
- :class:`OpenCodeStaleSessionError` — a 404 on a session we thought
  was live; the caller is expected to recreate the session and retry
  the call once.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

logger = logging.getLogger("scufris-bot.opencode_client")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OpenCodeError(Exception):
    """Base class for OpenCode client errors."""


class OpenCodeSessionError(OpenCodeError):
    """OpenCode REST or stream call failed (non-stale)."""


class OpenCodeStaleSessionError(OpenCodeError):
    """The cached session ID returned 404 — caller should recreate + retry."""


# ---------------------------------------------------------------------------
# Internal sentinels for chat_stream's queue
# ---------------------------------------------------------------------------


@dataclass
class _PostDone:
    """``POST /message`` returned 200 — wait for ``session.idle``."""


@dataclass
class _PostStale:
    """``POST /message`` returned 404 — session was GC'd by OpenCode."""


@dataclass
class _PostFailed:
    """``POST /message`` raised — surface the underlying error."""

    exc: BaseException


@dataclass
class _StreamEnded:
    """The ``/event`` SSE stream closed before ``session.idle`` arrived."""

    exc: Optional[BaseException] = None


@dataclass
class _BusReconnected:
    """Sentinel from :class:`_OpenCodeEventBus` after a reconnect.

    Subscribers see this dropped into their queue every time the shared
    upstream connection is re-established. The default consumer policy
    (in :meth:`OpenCodeClient.chat_stream`) is to surface this as an
    :class:`OpenCodeSessionError` because we cannot guarantee no events
    were lost during the reconnect window — callers that want to be
    fancier can do a state fetch via :meth:`OpenCodeClient.get_session`.
    """


# ---------------------------------------------------------------------------
# Event bus — single shared /event connection with per-session fan-out
# ---------------------------------------------------------------------------


class _OpenCodeEventBus:
    """Single shared ``GET /event`` connection with per-session fan-out.

    Why: ``opencode serve``'s ``/event`` bus is server-global — every
    connection gets every event for every session — so N concurrent
    chat turns opening N parallel SSE connections is just N copies of
    the same stream. Owning one persistent connection in the FastAPI
    lifespan and dispatching events to per-session queues collapses
    that down to 1.

    Lifecycle:

    - :meth:`start` spawns the background reader task. Idempotent.
    - :meth:`stop` cancels the reader and waits for it. Idempotent.
    - :meth:`subscribe` (async context manager) registers a queue for
      a given ``session_id`` and removes it on exit.

    The reader auto-reconnects with exponential backoff when the
    upstream connection drops. On every reconnect (after the first
    successful connect) a :class:`_BusReconnected` sentinel is
    broadcast to every live subscriber so consumers can choose to
    error out or refetch state.

    The bus drops events whose ``properties.sessionID`` is missing or
    has no matching subscriber — global events (``server.connected``,
    ``installation.*``, ``lsp.*``) carry no sessionID and our
    :class:`AgentManager` mapper ignores them anyway, so dropping them
    at the dispatch layer is loss-free.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 30.0,
        connect_timeout: float = 30.0,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        """
        Args:
            client: Shared :class:`httpx.AsyncClient` (owned by the
                outer :class:`OpenCodeClient`). The bus uses it but
                does NOT close it.
            reconnect_initial: First backoff delay, seconds.
            reconnect_max: Cap on the backoff delay, seconds.
            connect_timeout: How long :meth:`subscribe` waits for the
                bus to reach ``connected=True`` for the first time
                before raising :class:`OpenCodeSessionError`. Once
                the bus has connected at least once, subscribe is
                instantaneous (subsequent disconnects are surfaced
                via the :class:`_BusReconnected` sentinel mid-stream).
            logger_: Optional logger override (defaults to the module
                logger).
        """
        self._client = client
        self._reconnect_initial = reconnect_initial
        self._reconnect_max = reconnect_max
        self._connect_timeout = connect_timeout
        self._subs: Dict[str, List[asyncio.Queue[Any]]] = {}
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task[None]] = None
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
        self._task = asyncio.create_task(self._run(), name="opencode-event-bus")

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
    def stats(self) -> Dict[str, Any]:
        """Snapshot of bus health metrics for ``/v1/stats`` etc.

        Reads all counters atomically-enough (single-threaded asyncio
        loop) — no extra locking. ``max_queue_depth`` is the *current*
        max across live queues, not a rolling max.
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
        connect_timeout: Optional[float] = None,
    ) -> AsyncIterator[asyncio.Queue[Any]]:
        """Register a queue for ``session_id`` for the duration of the block.

        Yields an :class:`asyncio.Queue` onto which the bus drops
        events whose ``properties.sessionID`` matches ``session_id``.
        Subscribers may also put their own sentinels onto the queue —
        the bus only emits :class:`dict` events plus
        :class:`_BusReconnected` and never reads from the queue.

        Args:
            session_id: OpenCode session id to receive events for.
            connect_timeout: Override for the bus's default
                first-connect timeout. ``None`` uses the bus default.
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
            except asyncio.TimeoutError as exc:
                raise OpenCodeSessionError(
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
                    "opencode event bus: upstream stream closed cleanly; will reconnect"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — must keep loop alive
                self._logger.warning(
                    "opencode event bus: upstream error (%s); reconnect in %.1fs",
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
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self._reconnect_max)

    async def _read_once(self, *, emit_reconnect: bool) -> None:
        """Open one upstream connection and dispatch until it closes."""
        async with self._client.stream("GET", "/event", timeout=None) as resp:
            if resp.status_code >= 400:
                text = await resp.aread()
                raise OpenCodeSessionError(
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
                        "opencode event bus: dropping malformed SSE line"
                    )
                    continue
                if not isinstance(event, dict):
                    continue
                self._dispatch(event)

    def _dispatch(self, event: Dict[str, Any]) -> None:
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


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OpenCodeClient:
    """Async client for ``opencode serve``.

    Construct once per process. Use as an async context manager or call
    :meth:`aclose` explicitly during shutdown.
    """

    def __init__(
        self,
        base_url: str,
        *,
        provider_id: str = "github-copilot",
        model_id: str = "claude-sonnet-4",
        default_tools: Optional[Dict[str, bool]] = None,
        timeout: float = 30.0,
    ) -> None:
        """
        Args:
            base_url: Base URL of the OpenCode daemon
                (e.g. ``http://127.0.0.1:54321``). Trailing slashes are
                stripped.
            provider_id: Default provider for ``chat_stream`` requests.
            model_id: Default model for ``chat_stream`` requests.
            default_tools: Default tool allow/deny map merged into
                every ``chat_stream`` request. Per-call ``tools=``
                overrides individual entries.
            timeout: Read timeout (seconds) for short REST calls.
                Connect/write are pinned at 10s. ``chat_stream``
                overrides this with ``timeout=None`` because the model
                turn can take 10s+ and the SSE stream is long-lived.
        """
        self._base_url = base_url.rstrip("/")
        self._provider_id = provider_id
        self._model_id = model_id
        self._default_tools = dict(default_tools) if default_tools else None
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(
                connect=10.0,
                read=timeout,
                write=10.0,
                pool=10.0,
            ),
        )
        self._closed = False
        self._logger = logger
        # Optional fan-out for the /event bus. Stays ``None`` until
        # :meth:`start_event_bus` is called (lifespan does this in
        # production; tests skip it). When unset, :meth:`chat_stream`
        # falls back to the legacy per-request /event subscription.
        self._bus: Optional[_OpenCodeEventBus] = None

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def event_bus(self) -> Optional["_OpenCodeEventBus"]:
        """The shared event bus when started, else ``None``."""
        return self._bus

    async def start_event_bus(
        self,
        *,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 30.0,
        connect_timeout: float = 30.0,
    ) -> "_OpenCodeEventBus":
        """Start the shared ``/event`` listener. Idempotent.

        After this returns, every :meth:`chat_stream` call subscribes
        to the shared bus instead of opening its own ``GET /event``
        connection. The bus reconnects automatically on upstream
        failures with exponential backoff.

        Returns the bus so callers can read its :attr:`stats`. Closing
        the client (:meth:`aclose`) stops the bus before closing the
        underlying HTTP transport.
        """
        if self._bus is None:
            self._bus = _OpenCodeEventBus(
                self._client,
                reconnect_initial=reconnect_initial,
                reconnect_max=reconnect_max,
                connect_timeout=connect_timeout,
                logger_=self._logger,
            )
        await self._bus.start()
        return self._bus

    async def stop_event_bus(self) -> None:
        """Stop the shared ``/event`` listener (if started). Idempotent."""
        if self._bus is None:
            return
        await self._bus.stop()

    async def aclose(self) -> None:
        """Close the underlying HTTP client. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        # Stop the bus first so its reader doesn't error out during
        # transport teardown.
        if self._bus is not None:
            try:
                await self._bus.stop()
            except Exception:  # noqa: BLE001 — never block shutdown
                self._logger.exception("opencode event bus stop() raised")
        await self._client.aclose()

    async def __aenter__(self) -> "OpenCodeClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Session REST
    # ------------------------------------------------------------------

    async def create_session(self) -> Dict[str, Any]:
        """Create a new session.

        Body intentionally empty — system prompt, tools and provider/model
        are passed per-message in :meth:`chat_stream`, not at session
        create time, because the system prompt is dynamic (depends on
        the latest facts/summary for the user).
        """
        resp = await self._client.post("/session", json={})
        if resp.status_code >= 400:
            raise OpenCodeSessionError(
                f"create_session: {resp.status_code} {resp.text[:200]}"
            )
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise OpenCodeSessionError(
                f"create_session: malformed JSON: {exc}"
            ) from exc

    async def get_session(self, session_id: str) -> Dict[str, Any]:
        """Fetch a session by ID. 404 → :class:`OpenCodeStaleSessionError`."""
        resp = await self._client.get(f"/session/{session_id}")
        if resp.status_code == 404:
            raise OpenCodeStaleSessionError(session_id)
        if resp.status_code >= 400:
            raise OpenCodeSessionError(
                f"get_session({session_id}): {resp.status_code} {resp.text[:200]}"
            )
        return resp.json()

    async def list_sessions(self) -> List[Dict[str, Any]]:
        """List sessions for the current project (server-side filtered).

        Server returns either a bare list or a wrapped object — both
        shapes are normalised to ``list[dict]``.
        """
        resp = await self._client.get("/session")
        if resp.status_code >= 400:
            raise OpenCodeSessionError(
                f"list_sessions: {resp.status_code} {resp.text[:200]}"
            )
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("sessions"), list):
            return data["sessions"]
        raise OpenCodeSessionError(
            f"list_sessions: unexpected payload shape: {type(data).__name__}"
        )

    async def delete_session(self, session_id: str) -> None:
        """Delete a session. 404 is treated as success (already gone)."""
        resp = await self._client.delete(f"/session/{session_id}")
        if resp.status_code in (200, 204, 404):
            return
        if resp.status_code >= 400:
            raise OpenCodeSessionError(
                f"delete_session({session_id}): {resp.status_code} {resp.text[:200]}"
            )

    async def list_providers(self) -> Dict[str, Any]:
        """Fetch the provider/model catalogue (``GET /config/providers``)."""
        resp = await self._client.get("/config/providers")
        if resp.status_code >= 400:
            raise OpenCodeSessionError(
                f"list_providers: {resp.status_code} {resp.text[:200]}"
            )
        return resp.json()

    async def list_models(self) -> Dict[str, Any]:
        """Alias for :meth:`list_providers`.

        OpenCode bundles models inside the providers catalogue. Kept as
        a separate method so the proxy layer can name the route
        ``/v1/opencode/models`` without callers worrying about the
        upstream path.
        """
        return await self.list_providers()

    # ------------------------------------------------------------------
    # Chat with streaming events
    # ------------------------------------------------------------------

    def _build_chat_body(
        self,
        prompt: str,
        *,
        system: Optional[str],
        provider_id: Optional[str],
        model_id: Optional[str],
        tools: Optional[Dict[str, bool]],
    ) -> Dict[str, Any]:
        """Compose the ``POST /session/{id}/message`` body.

        Shared between :meth:`_chat_stream_legacy` and
        :meth:`_chat_stream_via_bus` so the wire format stays
        identical regardless of which event-source we use.
        """
        body: Dict[str, Any] = {
            "providerID": provider_id or self._provider_id,
            "modelID": model_id or self._model_id,
            "parts": [{"type": "text", "text": prompt}],
        }
        if system:
            body["system"] = system
        merged_tools = dict(self._default_tools) if self._default_tools else {}
        if tools:
            merged_tools.update(tools)
        if merged_tools:
            body["tools"] = merged_tools
        return body

    async def chat_stream(
        self,
        session_id: str,
        prompt: str,
        *,
        system: Optional[str] = None,
        provider_id: Optional[str] = None,
        model_id: Optional[str] = None,
        tools: Optional[Dict[str, bool]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Send a chat message and yield raw event dicts as they arrive.

        Subscribes to ``GET /event`` first, *then* issues
        ``POST /session/{id}/message`` so no events are missed for
        very fast turns. Yielded dicts are filtered to events whose
        ``properties.sessionID`` matches ``session_id`` (events with no
        sessionID, like ``server.connected``, are passed through too —
        the caller's mapper ignores them).

        When :meth:`start_event_bus` has been called the call routes
        through the shared bus (one upstream connection for the whole
        process). Otherwise it falls back to opening its own
        per-request ``GET /event`` connection — kept for tests and
        any caller that hasn't wired the bus yet.

        Terminates cleanly when ``session.idle`` arrives for our session.
        Raises:
            :class:`OpenCodeStaleSessionError`: on POST 404 (session GC'd).
            :class:`OpenCodeSessionError`: on ``session.error`` for our
                session, on REST/network failures, on stream closure
                before ``session.idle``, on subscription timeout, or
                on a bus reconnect mid-turn (lossy — caller may retry).
        """
        body = self._build_chat_body(
            prompt,
            system=system,
            provider_id=provider_id,
            model_id=model_id,
            tools=tools,
        )
        if self._bus is not None:
            async for event in self._chat_stream_via_bus(session_id, body):
                yield event
            return
        async for event in self._chat_stream_legacy(session_id, body):
            yield event

    async def _chat_stream_via_bus(
        self,
        session_id: str,
        body: Dict[str, Any],
    ) -> AsyncIterator[Dict[str, Any]]:
        """Bus-backed event source — single shared upstream connection.

        Subscription to the bus is instantaneous once the bus has
        connected at least once. The POST is fired in a background
        task so events stream concurrently with the model's turn (the
        POST itself blocks until ``session.idle``).

        On a bus reconnect mid-turn we cannot tell whether
        ``session.idle`` slipped through during the gap, so we surface
        :class:`OpenCodeSessionError` and let the caller decide
        whether to refetch state or retry. In practice this only
        fires when ``opencode serve`` itself bounces — rare enough
        that the simple "fail the turn" recovery beats a stateful
        replay loop.
        """
        assert self._bus is not None  # narrowed by chat_stream dispatch
        bus = self._bus

        async with bus.subscribe(session_id) as queue:
            # Subscription is registered. Now fire the POST in a
            # task — it blocks until the model finishes, but events
            # stream meanwhile via the bus.
            post_task: asyncio.Task[None] = asyncio.create_task(
                self._post_message(session_id, body, queue),
                name="opencode-poster",
            )
            try:
                async for event in self._drain_events(queue, session_id):
                    yield event
            finally:
                if not post_task.done():
                    post_task.cancel()
                try:
                    await post_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def _post_message(
        self,
        session_id: str,
        body: Dict[str, Any],
        queue: "asyncio.Queue[Any]",
    ) -> None:
        """POST the chat message and push a sentinel onto ``queue``.

        Shared by the legacy and bus-backed paths — both paths handle
        the same set of sentinels (:class:`_PostDone`,
        :class:`_PostStale`, :class:`_PostFailed`).
        """
        try:
            resp = await self._client.post(
                f"/session/{session_id}/message",
                json=body,
                timeout=None,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            await queue.put(_PostFailed(exc))
            return
        if resp.status_code == 404:
            await queue.put(_PostStale())
            return
        if resp.status_code >= 400:
            await queue.put(
                _PostFailed(
                    OpenCodeSessionError(
                        f"POST /session/{session_id}/message: "
                        f"{resp.status_code} {resp.text[:200]}"
                    )
                )
            )
            return
        await queue.put(_PostDone())

    async def _drain_events(
        self,
        queue: "asyncio.Queue[Any]",
        session_id: str,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Common consumer loop for legacy + bus paths.

        Iterates a queue that mixes raw event dicts (from either an
        ad-hoc reader task or the shared bus) with sentinel objects
        (:class:`_PostDone`, :class:`_PostStale`, :class:`_PostFailed`,
        :class:`_StreamEnded`, :class:`_BusReconnected`). Yields raw
        event dicts to the caller, terminates on ``session.idle``,
        raises on any failure mode.
        """
        while True:
            item = await queue.get()
            if isinstance(item, _PostDone):
                # POST returned 200; we keep iterating until
                # session.idle terminates the loop.
                continue
            if isinstance(item, _PostStale):
                raise OpenCodeStaleSessionError(session_id)
            if isinstance(item, _PostFailed):
                if isinstance(item.exc, OpenCodeError):
                    raise item.exc
                raise OpenCodeSessionError(
                    f"chat_stream: POST failed: {item.exc}"
                ) from item.exc
            if isinstance(item, _StreamEnded):
                if item.exc is not None:
                    raise OpenCodeSessionError(
                        f"chat_stream: event stream closed: {item.exc}"
                    ) from item.exc
                raise OpenCodeSessionError(
                    "chat_stream: event stream closed before session.idle"
                )
            if isinstance(item, _BusReconnected):
                # The shared bus reconnected mid-turn. We may have
                # missed events — fail the turn so the caller can
                # decide whether to refetch and synthesise a result.
                raise OpenCodeSessionError(
                    "chat_stream: event bus reconnected mid-turn; "
                    "events may have been lost"
                )
            if not isinstance(item, dict):
                continue
            props = item.get("properties") or {}
            etype = item.get("type")
            evt_session = props.get("sessionID")
            # Filter by sessionID for events that carry one. Events
            # without sessionID (server.connected, installation.*,
            # lsp.*, …) pass through in the legacy path; the bus
            # already drops them at the dispatch layer.
            if evt_session is not None and evt_session != session_id:
                continue
            yield item
            if etype == "session.idle" and evt_session == session_id:
                return
            if etype == "session.error" and evt_session == session_id:
                err = props.get("error") or {}
                if isinstance(err, dict):
                    name = err.get("name")
                    detail = err.get("data") or err.get("message") or err
                else:
                    name = None
                    detail = err
                raise OpenCodeSessionError(f"OpenCode session error ({name}): {detail}")

    async def _chat_stream_legacy(
        self,
        session_id: str,
        body: Dict[str, Any],
    ) -> AsyncIterator[Dict[str, Any]]:
        """Per-request ``GET /event`` subscription (pre-bus path).

        Kept for tests and for callers that haven't wired the bus.
        Behaviour is unchanged from the original implementation: open
        ``GET /event``, wait for the response, fire the POST in
        parallel, iterate until ``session.idle``.
        """
        queue: asyncio.Queue[Any] = asyncio.Queue()
        # Set by the reader once it has the /event response open. Also
        # set on reader failure so the main flow doesn't block forever.
        subscribed = asyncio.Event()

        async def _stream_reader() -> None:
            try:
                async with self._client.stream("GET", "/event", timeout=None) as resp:
                    if resp.status_code >= 400:
                        text = await resp.aread()
                        err = OpenCodeSessionError(
                            f"GET /event: {resp.status_code} "
                            f"{text.decode('utf-8', errors='replace')[:200]}"
                        )
                        await queue.put(_StreamEnded(err))
                        return
                    subscribed.set()
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
                                "opencode_client: dropping malformed SSE line"
                            )
                            continue
                        await queue.put(event)
                # Server closed the stream without an error.
                await queue.put(_StreamEnded(None))
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001
                await queue.put(_StreamEnded(exc))
            finally:
                subscribed.set()

        reader_task = asyncio.create_task(
            _stream_reader(), name="opencode-event-reader"
        )
        try:
            await asyncio.wait_for(subscribed.wait(), timeout=10.0)
        except asyncio.TimeoutError as exc:
            reader_task.cancel()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            raise OpenCodeSessionError(
                "chat_stream: timeout subscribing to /event"
            ) from exc

        post_task = asyncio.create_task(
            self._post_message(session_id, body, queue),
            name="opencode-poster",
        )

        try:
            async for event in self._drain_events(queue, session_id):
                yield event
        finally:
            for task in (post_task, reader_task):
                if not task.done():
                    task.cancel()
            for task in (post_task, reader_task):
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

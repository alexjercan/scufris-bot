"""Async HTTP client implementation.

Exposes :class:`ScufrisClient` (the public entry point) plus the four
exception classes the rest of the SDK and the CLI raise. This module
holds the JSON-endpoint methods (``healthz``, ``version``,
``resolve_identity``); the streaming (``chat_stream``) and state-
management (``sessions``, ``clear_session``, ``clear``) methods land
in a later step (#14 step 3) but will live in the same class so users
have one import path.

Design points worth flagging:

- **Transport injection.** ``ScufrisClient(transport=...)`` lets tests
  swap in :class:`httpx.MockTransport` without monkey-patching the
  module-level ``httpx`` import. Matches v1's pattern
  (``feature/opencode:scufris_client/client.py:199-215``); the hook
  is part of the public constructor signature so SDK consumers can
  use it too (e.g. to add request logging).
- **No bearer-token / no auth header.** Per #14 D2 the v0 client is
  unauth, mirroring the server's loopback-only trust model
  (ADR-8). When auth lands, add a ``token`` constructor parameter
  + the ``Authorization`` header here without breaking existing
  callers (the parameter defaults to ``None``).
- **Default URL via env or fallback.** ``base_url=None`` →
  ``$SCUFRIS_SERVER_URL`` if set, else
  :data:`DEFAULT_BASE_URL` (loopback 7080, v2's default port).
  Lets ad-hoc scripts run with no constructor args; the CLI
  passes ``base_url`` explicitly after parsing its own env.
- **Generous read timeout.** Mirrors v1
  (``feature/opencode:scufris_client/client.py:33``): 5s connect,
  300s read, 10s write, 5s pool. The 300s read budget exists for
  ``chat_stream`` (SSE reads can idle up to
  :data:`scufris_server.sse.KEEPALIVE_SECONDS` between frames);
  carrying it for JSON methods costs nothing and means one
  ``httpx.Timeout`` shared across all methods.
- **Error mapping pinned to v1 semantics.** Transport failures →
  :class:`ScufrisConnectionError`; 401/403 →
  :class:`ScufrisAuthError`; *any* other non-2xx →
  :class:`ScufrisServerError`. The TASK.md spec briefly mentioned
  "4xx → base :class:`ScufrisError`" — that was sloppy shorthand;
  v1 collapses 4xx-non-auth and 5xx into ``ScufrisServerError``
  because the CLI's UX doesn't distinguish them, and we follow v1
  here. The detail message preserves the status code so a 422 is
  still recognisable.
"""

from __future__ import annotations

import json as _json_lib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal

import httpx

#: Hardcoded fallback for the server base URL. Matches the default
#: ``scufris-server`` listens on (port 7080); v1 used 8765, but #14
#: D8 commits to the v2 default so a freshly-installed CLI talks to a
#: freshly-installed server with no env tweak.
DEFAULT_BASE_URL: str = "http://127.0.0.1:7080"

#: HTTP timeout profile shared across all client methods. The 300s
#: read budget is sized for ``chat_stream`` SSE idle windows; JSON
#: methods almost never need it but carrying one timeout object keeps
#: the client init simple.
DEFAULT_TIMEOUT: httpx.Timeout = httpx.Timeout(
    connect=5.0, read=300.0, write=10.0, pool=5.0
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ScufrisError(RuntimeError):
    """Base class for client-side errors.

    Subclasses split the broad "something went wrong" cases the CLI
    UX distinguishes (connection vs auth vs server-side problem).
    Callers that don't care about the split can catch this directly
    and treat every failure uniformly.
    """


class ScufrisConnectionError(ScufrisError):
    """The server was unreachable.

    Raised for transport-layer failures: refused connection, DNS
    failure, TLS handshake error, connect-timeout. Distinct from a
    valid HTTP response with an error status (those become
    :class:`ScufrisAuthError` / :class:`ScufrisServerError`).
    """


class ScufrisAuthError(ScufrisError):
    """Server returned 401 or 403.

    Reserved for when bearer-token auth lands; the v0 server is
    loopback-unauth (ADR-8) and never emits 401/403, but the SDK
    surface is forward-compatible so a future server upgrade
    doesn't break the exception taxonomy.
    """


class ScufrisServerError(ScufrisError):
    """Server returned a non-2xx status other than 401/403, or the
    response body was malformed.

    Covers 4xx (client errors: validation, missing route, etc.) and
    5xx (genuine server-side failures). The CLI UX treats both the
    same way — surface the status + detail to the user and let them
    decide what to do — so a single exception class is sufficient.
    Detail messages preserve the status code so callers can switch
    on it via string parsing if they really need to.
    """


# ---------------------------------------------------------------------------
# Streaming-event dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ThinkingEvent:
    """Client-side mirror of :class:`scufris_server.events.ThinkingEvent`.

    Verbatim field-set port — same names, same types, same optional-
    field semantics — so the v2 server can serialise a
    ``ThinkingEvent`` to JSON and the v2 SDK reconstructs it without
    field-name translation (#14 D13). Why a separate dataclass
    instead of importing the server's? Because the CLI must not
    depend on ``scufris_server`` — the SDK and CLI are intended to
    install alongside the server in development but ship as a thin
    client package that can talk to a remote server in production.
    A shared abstract package would be over-engineering for nine
    fields.

    Fields
    ------
    kind:
        Coarse event class. The :class:`Literal` here includes
        ``compaction`` for wire-compat with v1 even though v2 never
        emits it (server's events.py docstring at lines 89-90,
        137-140); the SDK has to accept it so a future server that
        relays a plugin-emitted compaction doesn't break older
        clients.
    source:
        Always ``"scufris"`` in v2 — kept as a field so the v1 wire
        format stays binary-compatible and a future plugin-emitted
        synthetic event source can carry its own label.
    text:
        Free-form content. Semantics depend on ``kind``: for
        ``text`` it's the streamed token chunk; for ``tool_call`` /
        ``tool_result`` it's the tool's technical name; for
        ``tool_meta`` it's the permission / annotation message.
    depth:
        Nesting level for indentation/styling. Always ``0`` in v2
        (single-agent post-swap; the field stays for v1 wire
        compatibility).
    arg, context, prior_turns, evicted, new_facts:
        Optional, included by the server only when populated. v2
        populates only ``arg`` (on ``tool_call`` events); the rest
        are reserved for forward compatibility with v1's richer
        sub-agent / compaction surface (see the server dataclass
        docstring for the long version).
    """

    kind: Literal["text", "tool_call", "tool_result", "tool_meta", "compaction"]
    source: str
    text: str
    depth: int
    arg: str | None = None
    context: str | None = None
    prior_turns: int | None = None
    evicted: int | None = None
    new_facts: int | None = None


@dataclass
class StreamEvent:
    """One event yielded by :meth:`ScufrisClient.chat_stream`.

    Discriminator-style dataclass (#14 D12): the ``kind`` field
    decides which optional fields are meaningful. The shape is a
    superset of v1's so existing renderers port cleanly.

    Per #14 D14, the SDK normalises the wire's ``done`` payload from
    ``message`` to :attr:`text` — v1 emitted ``text`` and our
    renderer port expects that name. The wire field is the server's
    business; renaming on the SDK boundary keeps the port mechanical.

    Fields by kind
    --------------
    ``kind == "thinking"``:
        :attr:`thinking` is populated with the decoded
        :class:`ThinkingEvent`; the rest are ``None``.
    ``kind == "done"``:
        :attr:`text` (the assistant reply, aliased from wire
        ``message``), :attr:`oc_session_id`, :attr:`oc_message_id`,
        :attr:`tokens` (``{"input": int, "output": int}``), and
        :attr:`cost` are populated. Exactly one ``done`` per
        successful stream.
    ``kind == "error"``:
        :attr:`error` (the message) and :attr:`error_type` (the
        server's :class:`Literal`-typed classifier, e.g.
        ``"OpencodeStaleSessionError"`` /
        ``"BusReconnected"``) are populated. Mutually exclusive
        with ``done``.

    Why a single flat dataclass instead of a discriminated union?
    Two reasons: (1) The CLI's main loop is one
    ``async for ev in stream`` — branching on ``ev.kind`` is the
    cleanest expression of that, and a union forces every branch
    to narrow with ``isinstance`` calls. (2) The wire shape is
    open — future server versions may add new optional fields
    (e.g. ``oc_assistant_id`` on ``done``) and a flat dataclass
    absorbs them without breaking older clients that just ignore
    unknown ``StreamEvent`` attributes (they don't access them).
    """

    kind: Literal["thinking", "done", "error"]
    thinking: ThinkingEvent | None = None
    # ``done`` fields (D14: ``text`` aliases wire ``message``).
    text: str | None = None
    oc_session_id: str | None = None
    oc_message_id: str | None = None
    tokens: dict[str, int] | None = None
    cost: float | None = None
    # ``error`` fields.
    error: str | None = None
    error_type: str | None = None


def _thinking_from_payload(data: dict[str, Any]) -> ThinkingEvent:
    """Reconstruct a :class:`ThinkingEvent` from the SSE JSON payload.

    Required fields (``kind``, ``source``, ``text``, ``depth``)
    come straight off the wire; optional fields default to ``None``
    when absent — the server omits them when unset per
    :meth:`scufris_server.events.ThinkingEvent.to_payload`, so the
    SDK must not require them to be present.

    A missing required field raises :class:`KeyError` from the
    bracket access, which the caller (:func:`_dispatch`) catches
    and re-raises as :class:`ScufrisServerError` with the original
    payload included so the operator can diagnose the wire-format
    drift.
    """
    return ThinkingEvent(
        kind=data["kind"],
        source=data["source"],
        text=data["text"],
        depth=int(data["depth"]),
        arg=data.get("arg"),
        context=data.get("context"),
        prior_turns=data.get("prior_turns"),
        evicted=data.get("evicted"),
        new_facts=data.get("new_facts"),
    )


def _dispatch(event_name: str, payload_text: str) -> StreamEvent:
    """Turn one parsed SSE record into a :class:`StreamEvent`.

    The ``event:`` line value selects the branch; the ``data:`` block
    is parsed as JSON. Malformed JSON raises
    :class:`ScufrisServerError` — the SSE stream is now invalid and
    the caller's iteration must stop.

    Unknown ``event_name`` values are surfaced as a synthetic error
    event rather than silently dropped (matches v1 at
    ``feature/opencode:scufris_client/client.py:144-150``); this
    avoids a hang where the consumer is waiting for a terminal event
    that never arrives because the server is speaking an event
    vocabulary the SDK doesn't understand.
    """
    try:
        payload = _json_lib.loads(payload_text) if payload_text else {}
    except _json_lib.JSONDecodeError as exc:
        raise ScufrisServerError(
            f"malformed SSE payload for event {event_name!r}: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise ScufrisServerError(
            f"expected JSON object in SSE payload for event "
            f"{event_name!r}, got {type(payload).__name__}"
        )

    if event_name == "thinking":
        try:
            thinking = _thinking_from_payload(payload)
        except KeyError as exc:
            raise ScufrisServerError(
                f"missing required field {exc.args[0]!r} in thinking event: {payload!r}"
            ) from exc
        return StreamEvent(kind="thinking", thinking=thinking)

    if event_name == "done":
        # Server uses ``message`` on the wire; SDK normalises to
        # ``text`` per D14 so the renderer port is mechanical.
        tokens_raw = payload.get("tokens")
        tokens: dict[str, int] | None = None
        if isinstance(tokens_raw, dict):
            tokens = {
                "input": int(tokens_raw.get("input", 0)),
                "output": int(tokens_raw.get("output", 0)),
            }
        return StreamEvent(
            kind="done",
            text=payload.get("message", ""),
            oc_session_id=payload.get("oc_session_id"),
            oc_message_id=payload.get("oc_message_id"),
            tokens=tokens,
            cost=payload.get("cost"),
        )

    if event_name == "error":
        return StreamEvent(
            kind="error",
            error=payload.get("error", "unknown error"),
            error_type=payload.get("error_type"),
        )

    return StreamEvent(
        kind="error",
        error=f"unexpected SSE event: {event_name!r}",
        error_type="UnknownSSEEvent",
    )


async def _parse_sse_stream(
    lines: AsyncIterator[str],
) -> AsyncIterator[StreamEvent]:
    """Parse a decoded-line stream into :class:`StreamEvent`s.

    Implements the subset of the SSE spec scufris-server emits:

    - ``event:`` lines set the event type for the next dispatch.
    - ``data:`` lines accumulate; multi-line ``data:`` is joined
      with ``\\n`` (per spec).
    - Blank lines dispatch (build a :class:`StreamEvent` from the
      buffered ``event`` + ``data``, reset the buffers).
    - Comment lines (``:`` prefix, includes ``: keepalive``) are
      dropped.
    - Unknown field lines (``id:``, ``retry:``, etc.) are dropped
      per spec — scufris-server never emits them.

    The parser also dispatches any trailing buffered event that
    didn't have a final blank line (defensive; server always sends
    the trailing blank in :func:`scufris_server.sse.format_event`).

    Takes an :class:`AsyncIterator[str]` rather than the raw
    response so the parser is unit-testable without an httpx
    response object — the caller wraps ``response.aiter_lines()``.
    """
    event_name = ""
    data_buf: list[str] = []

    async for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if event_name or data_buf:
                yield _dispatch(event_name, "\n".join(data_buf))
            event_name = ""
            data_buf = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
            continue
        if line.startswith("data:"):
            data_buf.append(line[len("data:") :].lstrip(" "))
            continue
        # Unknown SSE field — drop per spec.

    if event_name or data_buf:
        yield _dispatch(event_name, "\n".join(data_buf))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ScufrisClient:
    """Async HTTP client for the scufris-server v2 surface.

    Owns an :class:`httpx.AsyncClient` for connection pooling. Use
    as an async context manager so the underlying pool gets closed
    cleanly, or call :meth:`aclose` explicitly.

    Parameters
    ----------
    base_url:
        Server base URL. If ``None`` (default), the constructor
        reads ``$SCUFRIS_SERVER_URL``; if that's also unset, falls
        back to :data:`DEFAULT_BASE_URL`. Trailing slash is
        normalised away.
    timeout:
        Optional :class:`httpx.Timeout` override. Defaults to
        :data:`DEFAULT_TIMEOUT` (5s connect, 300s read, 10s write,
        5s pool).
    transport:
        Optional :class:`httpx.AsyncBaseTransport` override for
        testing (:class:`httpx.MockTransport` is the typical
        choice). The SDK's own tests use this; consumer tests can
        too.

    Methods present:

    - :meth:`healthz` — GET /v1/healthz
    - :meth:`version` — GET /v1/version
    - :meth:`resolve_identity` — POST /v1/identity/resolve
    - :meth:`chat_stream` — POST /v1/chat/stream (SSE)
    - :meth:`sessions` — GET /v1/sessions
    - :meth:`clear_session` — POST /v1/sessions/{channel_id}/clear
    - :meth:`clear` — POST /v1/clear

    Notably absent: ``/v1/chat`` (non-streaming). Per #14 D2 the
    SDK is stream-only; the synchronous endpoint is reachable from
    HTTP but not from this SDK because the CLI never needs it (it
    always wants the thinking trail).
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: httpx.Timeout | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        resolved_url = (
            base_url or os.environ.get("SCUFRIS_SERVER_URL") or DEFAULT_BASE_URL
        )
        self.base_url: str = resolved_url.rstrip("/")
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Accept": "application/json"},
            timeout=timeout or DEFAULT_TIMEOUT,
            transport=transport,
        )

    async def __aenter__(self) -> ScufrisClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying httpx client and release the pool.

        Safe to call multiple times; httpx's own ``aclose`` is
        idempotent.
        """
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def healthz(self) -> dict[str, Any]:
        """``GET /v1/healthz`` — server liveness + opencode status.

        Always returns 200 from the server (opencode failure is
        reported as data, not HTTP status — see
        :mod:`scufris_server.routes.health`). The response body
        shape is::

            {
                "ok": bool,
                "opencode": {"healthy": bool, "version": str}
                          | {"error": str},
            }

        Raises
        ------
        ScufrisConnectionError
            Server unreachable.
        ScufrisServerError
            Unexpected non-2xx (shouldn't happen on a healthy
            server but possible on misconfiguration).
        """
        return await self._json("GET", "/v1/healthz")

    async def version(self) -> dict[str, Any]:
        """``GET /v1/version`` — scufris + opencode versions.

        Response shape::

            {
                "version": str,            # scufris-server version
                "opencode_version": str | None,
            }

        ``opencode_version`` is ``None`` if the server's cache is
        empty AND a live probe of opencode failed; otherwise the
        cached value.

        Raises
        ------
        ScufrisConnectionError
            Server unreachable.
        ScufrisServerError
            Unexpected non-2xx.
        """
        return await self._json("GET", "/v1/version")

    async def resolve_identity(self, surface: str, surface_id: str) -> dict[str, Any]:
        """``POST /v1/identity/resolve`` — map (surface, surface_id) → user.

        Materialises the ``users`` + ``surface_bindings`` rows on
        first contact (TOML hit or default fallback). Response
        shape matches
        :class:`scufris_server.identity.ResolvedUser`::

            {
                "user_id": int,
                "username": str,
                "surface": str,
                "surface_id": str,
                "bound_surfaces": [
                    {"surface": str, "surface_id": str},
                    ...
                ],
            }

        Parameters
        ----------
        surface:
            One of ``"cli"``, ``"telegram"``, ``"web"``, ... Must
            be non-empty (server enforces).
        surface_id:
            Per-surface identifier (terminal user, telegram
            chat_id, etc.). Must be non-empty (server enforces).

        Raises
        ------
        ScufrisConnectionError
            Server unreachable.
        ScufrisServerError
            422 (empty surface/id), 500 (mis-seeded DB), or any
            other non-2xx. The detail message preserves the
            status code.
            Also raised if ``user_id`` is missing from the response
            body (defensive — server should always include it).
        """
        body = await self._json(
            "POST",
            "/v1/identity/resolve",
            json={"surface": surface, "surface_id": surface_id},
        )
        if not isinstance(body.get("user_id"), int):
            raise ScufrisServerError(f"missing 'user_id' in identity reply: {body!r}")
        return body

    async def chat_stream(
        self,
        surface: str,
        surface_id: str,
        agent: str,
        message: str,
    ) -> AsyncIterator[StreamEvent]:
        """``POST /v1/chat/stream`` — stream one turn of chat as SSE.

        Async generator. Yields :class:`StreamEvent`s as they arrive
        on the wire:

        - Zero or more ``thinking`` events (model text chunks, tool
          calls, tool results, permission events).
        - Exactly one terminal event: either ``done`` (success;
          carries the full reply text plus session metadata) or
          ``error`` (the server caught a failure mid-stream and
          surfaced it through SSE rather than HTTP — see the
          chat_stream route docstring at
          :mod:`scufris_server.routes.chat_stream` for the full list).

        The underlying SSE connection is closed in the ``finally``
        block — including when the caller stops iterating early
        (e.g. on Ctrl-C). The server-side route handler responds to
        the client disconnect by cancelling the background
        ``send_message`` task.

        Parameters
        ----------
        surface, surface_id:
            Channel coordinates. Server uses ``(surface, surface_id,
            agent)`` to locate or create a per-user opencode session
            (see :class:`scufris_server.sessions.Channel`).
        agent:
            opencode agent name — ``"build"``, ``"plan"``, etc. The
            CLI hardcodes ``"build"`` per #14 D3; SDK accepts any
            string and lets the server validate.
        message:
            The user prompt. Server enforces ``min_length=1``; an
            empty string raises :class:`ScufrisServerError` (422).

        Raises (pre-stream — before any event is yielded)
        -------------------------------------------------
        ScufrisConnectionError
            Server unreachable.
        ScufrisServerError
            503 with structured body (DefaultModelMissing,
            OpencodeNetworkError, OpencodeServerError) or any other
            non-2xx before the SSE body starts. Wire shape is
            ``{"error": str, "error_type": str}`` — preserved in
            the exception message.

        Mid-stream errors do *not* raise; they arrive as
        ``StreamEvent(kind="error", error=..., error_type=...)``
        events. The route docstring explains why: once the HTTP
        headers are on the wire, FastAPI can't switch to a 5xx
        response, so all post-commit failures must be tunnelled
        through SSE.
        """
        try:
            req = self._client.build_request(
                "POST",
                "/v1/chat/stream",
                json={
                    "message": message,
                    "channel": {
                        "surface": surface,
                        "surface_id": surface_id,
                        "agent": agent,
                    },
                },
                headers={"Accept": "text/event-stream"},
            )
            response = await self._client.send(req, stream=True)
        except httpx.ConnectError as exc:
            raise ScufrisConnectionError(
                f"could not connect to {self.base_url}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ScufrisServerError(str(exc)) from exc

        try:
            # Pre-stream status check — must run before we start
            # iterating the body so 503s with structured JSON are
            # surfaced as exceptions rather than parsed as malformed
            # SSE. ``_raise_for_status`` reads ``response.text``,
            # which on a stream=True response requires aread() first.
            if response.status_code >= 400:
                await response.aread()
                self._raise_for_status(response)

            async def _lines() -> AsyncIterator[str]:
                # ``aiter_lines`` decodes UTF-8 and splits on \n;
                # exactly what _parse_sse_stream wants.
                async for line in response.aiter_lines():
                    yield line

            async for event in _parse_sse_stream(_lines()):
                yield event
        finally:
            await response.aclose()

    async def sessions(self, user_id: int) -> list[dict[str, Any]]:
        """``GET /v1/sessions?user_id=N`` — list channels for a user.

        Returns a list of channel rows, most-recent first. Each row
        has shape::

            {
                "channel_id": int,
                "channel": {"surface": str, "surface_id": str, "agent": str},
                "oc_session_id": str,
                "last_used_at": int,            # unix seconds
                "title": str | None,            # null if opencode unreachable
                "tokens": {"input": int, "output": int} | None,
                "cost": float | None,
            }

        Empty list when the user has no linked channels — not a
        404. See :func:`scufris_server.routes.sessions.list_sessions`
        for the full degradation contract.

        Raises
        ------
        ScufrisConnectionError
            Server unreachable.
        ScufrisServerError
            404 if ``user_id`` doesn't exist; other non-2xx pass
            through with the status code in the message.
        """
        return await self._json_list("GET", "/v1/sessions", params={"user_id": user_id})

    async def clear_session(self, channel_id: int) -> dict[str, Any]:
        """``POST /v1/sessions/{channel_id}/clear`` — drop one channel's link.

        Idempotent. Response::

            {"cleared": bool}

        ``cleared`` is ``True`` if a ``session_links`` row was
        removed, ``False`` if the channel existed but had no live
        link (already cleared, or fresh channel with no chat). Both
        cases return 200.

        Per ADR-13 the underlying opencode session is preserved;
        only the local link is dropped. The next chat to this
        channel allocates a fresh opencode session.

        Raises
        ------
        ScufrisConnectionError
            Server unreachable.
        ScufrisServerError
            404 if the channel doesn't exist or isn't owned by the
            resolved principal (override > default user).
        """
        return await self._json("POST", f"/v1/sessions/{channel_id}/clear")

    async def clear(self, user_id: int) -> dict[str, Any]:
        """``POST /v1/clear`` — drop every session_link for a user.

        Body shape on the wire is ``{"user_id": int}``. Response::

            {"count": int}

        ``count == 0`` is a success (user exists, had no live links
        to clear), not a 404. Channel rows are preserved — only
        ``session_links`` rows are removed, matching the per-channel
        clear's idempotency story.

        Per #14 D11 this is what the CLI's bare ``/clear`` command
        maps to (matches v1's effect).

        Raises
        ------
        ScufrisConnectionError
            Server unreachable.
        ScufrisServerError
            404 if ``user_id`` doesn't exist; 422 if the body fails
            validation (``user_id < 1``).
        """
        return await self._json("POST", "/v1/clear", json={"user_id": user_id})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue an HTTP request and decode the JSON response.

        Centralises the transport-error → :class:`ScufrisConnectionError`
        mapping, status-code check (via :meth:`_raise_for_status`),
        and JSON-decode + dict-shape guard. Every JSON endpoint
        method delegates here.

        Returns the decoded body. Raises one of the four
        ``Scufris*`` exception classes on failure.
        """
        try:
            response = await self._client.request(
                method, path, params=params, json=json
            )
        except httpx.ConnectError as exc:
            raise ScufrisConnectionError(
                f"could not connect to {self.base_url}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            # Catches TimeoutException, ReadError, RemoteProtocolError, etc.
            # — anything httpx considers a transport-layer issue that
            # didn't surface as ConnectError. v1 categorised these as
            # ServerError; we follow.
            raise ScufrisServerError(str(exc)) from exc

        self._raise_for_status(response)

        try:
            data = response.json()
        except ValueError as exc:
            raise ScufrisServerError(f"non-JSON response from {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ScufrisServerError(
                f"expected JSON object from {path}, got {type(data).__name__}"
            )
        return data

    async def _json_list(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Sibling of :meth:`_json` for endpoints returning a list.

        Only :meth:`sessions` uses this so far; lifted to a helper
        rather than inlined so the error-mapping policy (transport
        failures, status check, JSON-decode, shape guard) stays
        single-sourced with the dict path. Returns a list of dicts —
        the SDK doesn't introspect element shape; callers (CLI
        renderer) read fields by name.

        A non-list top-level body raises :class:`ScufrisServerError`
        for the same reason :meth:`_json` rejects non-dicts: silent
        type confusion downstream is worse than a clear failure
        here. Per-element shape isn't validated — that would
        require a Pydantic model per endpoint, which we deferred.
        """
        try:
            response = await self._client.request(
                method, path, params=params, json=json
            )
        except httpx.ConnectError as exc:
            raise ScufrisConnectionError(
                f"could not connect to {self.base_url}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ScufrisServerError(str(exc)) from exc

        self._raise_for_status(response)

        try:
            data = response.json()
        except ValueError as exc:
            raise ScufrisServerError(f"non-JSON response from {path}: {exc}") from exc
        if not isinstance(data, list):
            raise ScufrisServerError(
                f"expected JSON array from {path}, got {type(data).__name__}"
            )
        return data

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        """Translate a non-2xx response into the right exception.

        2xx returns silently. 401/403 raise
        :class:`ScufrisAuthError`. Any other non-2xx raises
        :class:`ScufrisServerError`. The detail message tries to
        read a structured body (``detail`` per FastAPI convention,
        ``error`` per scufris's own error envelopes); falls back
        to response text or reason phrase if neither is present.
        """
        if response.status_code < 400:
            return
        detail: str
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = payload.get("detail") or payload.get("error") or str(payload)
            else:
                detail = str(payload)
        except ValueError:
            detail = response.text or response.reason_phrase or ""
        if response.status_code in (401, 403):
            raise ScufrisAuthError(f"{response.status_code}: {detail}")
        raise ScufrisServerError(f"{response.status_code}: {detail}")

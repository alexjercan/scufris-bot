"""Unit tests for :mod:`scufris_client.client`.

Uses :class:`httpx.MockTransport` for transport injection (matches
the v1 SDK test pattern at
``feature/opencode:tests/test_cli_client.py``). No real network
traffic happens; respx isn't needed because the SDK exposes a
``transport`` constructor parameter that makes mocking explicit and
deterministic.

Coverage (#14 steps 2-3):

- Each JSON-endpoint method's happy path round-trips a body.
- ``resolve_identity`` defensive check raises on missing ``user_id``.
- Error-mapping: ``ConnectError`` → ``ScufrisConnectionError``,
  401 / 403 → ``ScufrisAuthError``, other 4xx / 5xx →
  ``ScufrisServerError``.
- Constructor defaults: env-var resolution, hardcoded fallback,
  trailing-slash normalisation.
- ``async with`` lifecycle closes the underlying httpx client.
- ``chat_stream`` end-to-end SSE round-trip: thinking + done /
  error terminals, keepalive comments, multi-line data,
  unknown-event synthesis, malformed payload, pre-stream 503,
  pre-stream connect error, request-body shape.
- ``sessions`` empty + populated + user_id query param + non-list
  body rejection.
- ``clear_session`` cleared-true/false.
- ``clear`` request-body shape + count response + zero-count success.
- Direct ``_parse_sse_stream`` edge cases: multiline data, trailing
  event without final blank, unknown fields dropped.

Section layout: Helpers → constructor → async-cm → endpoint groups
(healthz, version, resolve_identity, chat_stream, sessions, clear)
→ shared error-mapping → exception hierarchy.
"""

from __future__ import annotations

import asyncio
import json as _json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from scufris_client import (
    ScufrisAuthError,
    ScufrisClient,
    ScufrisConnectionError,
    ScufrisError,
    ScufrisServerError,
)
from scufris_client.client import DEFAULT_BASE_URL, _parse_sse_stream

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    base_url: str = "http://test",
) -> ScufrisClient:
    """Build a ScufrisClient wired to a MockTransport handler.

    ``base_url`` defaults to a non-routable hostname so any
    accidental real-network access blows up loudly rather than
    silently hitting localhost.
    """
    return ScufrisClient(
        base_url=base_url,
        transport=httpx.MockTransport(handler),
    )


def _run(coro: object) -> object:
    """Sync wrapper for the async tests — avoids the pytest-asyncio
    plugin dependency for a handful of trivially-isolated tests."""
    return asyncio.run(coro)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


def test_constructor_uses_explicit_base_url() -> None:
    client = ScufrisClient(base_url="http://explicit:9999")
    assert client.base_url == "http://explicit:9999"
    _run(client.aclose())


def test_constructor_reads_env_when_base_url_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCUFRIS_SERVER_URL", "http://from-env:1234")
    client = ScufrisClient()
    assert client.base_url == "http://from-env:1234"
    _run(client.aclose())


def test_constructor_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCUFRIS_SERVER_URL", raising=False)
    client = ScufrisClient()
    assert client.base_url == DEFAULT_BASE_URL
    assert client.base_url == "http://127.0.0.1:7080"
    _run(client.aclose())


def test_constructor_strips_trailing_slash() -> None:
    client = ScufrisClient(base_url="http://host:7080/")
    assert client.base_url == "http://host:7080"
    _run(client.aclose())


# ---------------------------------------------------------------------------
# Async context manager
# ---------------------------------------------------------------------------


def test_async_context_manager_closes_client() -> None:
    """`async with ScufrisClient(...)` calls aclose on exit.

    Verifies the contract by re-entering aclose (idempotent) after
    the context exits — if `__aexit__` already closed it, the
    httpx client's `is_closed` flag is set.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async def go() -> bool:
        client = _make_client(handler)
        async with client:
            await client.healthz()
        return client._client.is_closed

    assert _run(go()) is True


# ---------------------------------------------------------------------------
# /v1/healthz
# ---------------------------------------------------------------------------


def test_healthz_returns_body() -> None:
    expected = {
        "ok": True,
        "opencode": {"healthy": True, "version": "1.15.13"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/healthz"
        return httpx.Response(200, json=expected)

    async def go() -> dict[str, object]:
        async with _make_client(handler) as client:
            return await client.healthz()

    assert _run(go()) == expected


# ---------------------------------------------------------------------------
# /v1/version
# ---------------------------------------------------------------------------


def test_version_returns_body() -> None:
    expected = {"version": "0.1.0", "opencode_version": "1.15.13"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/version"
        return httpx.Response(200, json=expected)

    async def go() -> dict[str, object]:
        async with _make_client(handler) as client:
            return await client.version()

    assert _run(go()) == expected


def test_version_handles_null_opencode_version() -> None:
    """When the server can't reach opencode, ``opencode_version``
    is ``null`` on the wire (→ ``None`` in Python). The SDK passes
    it through unchanged."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"version": "0.1.0", "opencode_version": None})

    async def go() -> dict[str, object]:
        async with _make_client(handler) as client:
            return await client.version()

    body = _run(go())
    assert isinstance(body, dict)
    assert body["opencode_version"] is None


# ---------------------------------------------------------------------------
# /v1/identity/resolve
# ---------------------------------------------------------------------------


def test_resolve_identity_round_trips() -> None:
    sent: dict[str, object] = {}
    expected = {
        "user_id": 2,
        "username": "alex",
        "surface": "cli",
        "surface_id": "alex",
        "bound_surfaces": [
            {"surface": "cli", "surface_id": "alex"},
            {"surface": "telegram", "surface_id": "8231376426"},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/identity/resolve"
        sent["body"] = _json.loads(request.content.decode())
        return httpx.Response(200, json=expected)

    async def go() -> dict[str, object]:
        async with _make_client(handler) as client:
            return await client.resolve_identity("cli", "alex")

    body = _run(go())
    assert body == expected
    assert sent["body"] == {"surface": "cli", "surface_id": "alex"}


def test_resolve_identity_raises_on_missing_user_id() -> None:
    """Server is expected to always include ``user_id``; if it
    doesn't, the SDK refuses to silently corrupt downstream calls
    that depend on the field."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"username": "alex"})

    async def go() -> None:
        async with _make_client(handler) as client:
            await client.resolve_identity("cli", "alex")

    with pytest.raises(ScufrisServerError, match="missing 'user_id'"):
        _run(go())


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def test_connection_error_on_transport_failure() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async def go() -> None:
        async with _make_client(handler) as client:
            await client.healthz()

    with pytest.raises(ScufrisConnectionError, match="could not connect"):
        _run(go())


def test_other_transport_error_becomes_server_error() -> None:
    """httpx raises ``ReadError`` / ``TimeoutException`` / ... for
    non-connect transport problems; v1 categorises those as
    ``ScufrisServerError`` (the connection went *somewhere* — it
    just failed mid-flight)."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    async def go() -> None:
        async with _make_client(handler) as client:
            await client.healthz()

    with pytest.raises(ScufrisServerError, match="read timed out"):
        _run(go())


@pytest.mark.parametrize("status", [401, 403])
def test_auth_error_on_401_403(status: int) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "no soup for you"})

    async def go() -> None:
        async with _make_client(handler) as client:
            await client.healthz()

    with pytest.raises(ScufrisAuthError, match=f"{status}: no soup for you"):
        _run(go())


@pytest.mark.parametrize("status", [400, 404, 422, 500, 502, 503])
def test_server_error_on_other_non_2xx(status: int) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "boom"})

    async def go() -> None:
        async with _make_client(handler) as client:
            await client.healthz()

    with pytest.raises(ScufrisServerError, match=f"{status}: boom"):
        _run(go())


def test_server_error_falls_back_to_text_when_no_json() -> None:
    """Some 5xx responses (e.g. uvicorn's bare 500) have plain-text
    bodies; the SDK should still surface them as
    ``ScufrisServerError`` with the text in the message."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    async def go() -> None:
        async with _make_client(handler) as client:
            await client.healthz()

    with pytest.raises(ScufrisServerError, match="500: Internal Server Error"):
        _run(go())


def test_server_error_prefers_error_field_when_no_detail() -> None:
    """Some scufris error envelopes use ``error`` (e.g.
    ``ChatErrorBody``); the SDK reads that as a fallback for FastAPI's
    standard ``detail`` field."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"error": "opencode unavailable", "error_type": "OpencodeUnavailable"},
        )

    async def go() -> None:
        async with _make_client(handler) as client:
            await client.healthz()

    with pytest.raises(ScufrisServerError, match="503: opencode unavailable"):
        _run(go())


def test_non_json_body_raises_server_error() -> None:
    """If the server returns 200 with a non-JSON body (shouldn't
    happen but be defensive), the SDK raises rather than corrupting
    downstream code with garbage."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    async def go() -> None:
        async with _make_client(handler) as client:
            await client.healthz()

    with pytest.raises(ScufrisServerError, match="non-JSON response"):
        _run(go())


def test_non_dict_json_body_raises_server_error() -> None:
    """The SDK contracts ``_json`` to return ``dict[str, Any]``;
    a top-level array or scalar would violate that and is
    rejected rather than silently mis-typed."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "dict"])

    async def go() -> None:
        async with _make_client(handler) as client:
            await client.healthz()

    with pytest.raises(ScufrisServerError, match="expected JSON object"):
        _run(go())


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


def test_all_errors_inherit_from_scufris_error() -> None:
    """``ScufrisError`` is the catch-all base; callers that don't
    care about the split can use it as a single handler."""
    assert issubclass(ScufrisConnectionError, ScufrisError)
    assert issubclass(ScufrisAuthError, ScufrisError)
    assert issubclass(ScufrisServerError, ScufrisError)
    assert issubclass(ScufrisError, RuntimeError)


# ---------------------------------------------------------------------------
# SSE streaming transport helpers
# ---------------------------------------------------------------------------


class _AsyncByteStream(httpx.AsyncByteStream):
    """An async byte stream backed by a list of byte chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def __iter__(self):
        yield from self._chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        pass


class _SSETransport(httpx.AsyncBaseTransport):
    """Transport that returns a single streaming SSE response.

    ``events`` is a list of ``(name, payload_dict)`` tuples. Each
    pair is encoded into a wire-format SSE record
    ``event: NAME\ndata: JSON\n\n`` using the server's own
    :func:`scufris_server.sse.format_event` convention so the
    wire shape matches production exactly.
    """

    def __init__(self, events: list[tuple[str, dict[str, object]]]) -> None:  # noqa: UP006
        chunks: list[bytes] = []
        for name, payload in events:
            data_json = _json.dumps(payload, separators=(",", ":"))
            chunks.append(f"event: {name}\ndata: {data_json}\n\n".encode())
        self._stream = _AsyncByteStream(chunks)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=self._stream,
            headers={"Content-Type": "text/event-stream"},
        )


class _JSONTransport(httpx.AsyncBaseTransport):
    """Transport that returns a single JSON response (no streaming).

    Handy when you don't want to drag in ``httpx.MockTransport`` for
    a single-call JSON endpoint test.
    """

    def __init__(
        self, status: int = 200, json_body: dict[str, object] | None = None
    ) -> None:
        self._status = status
        self._body = json_body or {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self._status, json=self._body)


class _FailingTransport(httpx.AsyncBaseTransport):
    """Transport that always raises ``httpx.ConnectError``."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")


def _make_sse_client(
    events: list[tuple[str, dict[str, object]]],
    *,
    base_url: str = "http://test",
) -> ScufrisClient:
    """Build a ScufrisClient wired to an SSE streaming transport."""
    return ScufrisClient(base_url=base_url, transport=_SSETransport(events))


# ---------------------------------------------------------------------------
# /v1/chat/stream
# ---------------------------------------------------------------------------


def test_chat_stream_happy_path() -> None:
    """Server returns thinking + thinking + done.

    Verifies we get 3 events, the request body shape, and the
    ``Accept: text/event-stream`` header.
    """
    sent_body: dict[str, dict] = {}
    sent_accept: str = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_accept
        sent_body["_"] = _json.loads(request.content.decode())
        sent_accept = request.headers.get("accept", "")
        return httpx.Response(
            200,
            stream=_AsyncByteStream(
                [
                    (
                        "event: thinking\ndata: "
                        + _json.dumps(
                            {
                                "kind": "text",
                                "source": "scufris",
                                "text": "Hello",
                                "depth": 0,
                            },
                            separators=(",", ":"),
                        )
                        + "\n\n"
                    ).encode(),
                    (
                        "event: thinking\ndata: "
                        + _json.dumps(
                            {
                                "kind": "text",
                                "source": "scufris",
                                "text": " world",
                                "depth": 0,
                            },
                            separators=(",", ":"),
                        )
                        + "\n\n"
                    ).encode(),
                    (
                        "event: done\ndata: "
                        + _json.dumps(
                            {
                                "message": "Hello world",
                                "oc_session_id": "ses_abc",
                                "oc_message_id": "msg_456",
                                "tokens": {"input": 10, "output": 5},
                                "cost": 0.01,
                            },
                            separators=(",", ":"),
                        )
                        + "\n\n"
                    ).encode(),
                ]
            ),
            headers={"Content-Type": "text/event-stream"},
        )

    client = ScufrisClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    )

    async def go() -> list[Any]:
        evts = []
        async for ev in client.chat_stream("cli", "user1", "build", "hi"):
            evts.append(ev)
        return evts

    evts: list[Any] = _run(go())  # type: ignore[assignment]
    assert len(evts) == 3
    assert evts[0].kind == "thinking"
    assert evts[0].thinking.text == "Hello"
    assert evts[1].kind == "thinking"
    assert evts[1].thinking.text == " world"
    assert evts[2].kind == "done"
    assert evts[2].text == "Hello world"
    assert sent_body["_"]["message"] == "hi"
    assert sent_body["_"]["channel"] == {
        "surface": "cli",
        "surface_id": "user1",
        "agent": "build",
    }
    assert sent_accept == "text/event-stream"
    _run(client.aclose())


def test_chat_stream_error_terminal() -> None:
    """Server emits thinking then error.

    The second event should be a ``StreamEvent(kind="error")`` with
    the payload's ``error`` and ``error_type`` fields.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_AsyncByteStream(
                [
                    b'event: thinking\ndata: {"kind":"text","source":"scufris","text":"Thinking","depth":0}\n\n',
                    b'event: error\ndata: {"error":"opencode timed out","error_type":"OpencodeNetworkError"}\n\n',
                ]
            ),
            headers={"Content-Type": "text/event-stream"},
        )

    client = ScufrisClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    )

    async def go() -> list[dict[str, object]]:
        evts = []
        async for ev in client.chat_stream("cli", "u1", "build", "test"):
            evts.append(ev.__dict__)
        return evts

    evts: list[Any] = _run(go())  # type: ignore[assignment]
    assert len(evts) == 2
    assert evts[0]["kind"] == "thinking"
    assert evts[1]["kind"] == "error"
    assert evts[1]["error"] == "opencode timed out"
    assert evts[1]["error_type"] == "OpencodeNetworkError"
    _run(client.aclose())


def test_chat_stream_pre_stream_503() -> None:
    """Server returns 503 before the SSE body starts.

    The SDK reads the status code before iterating, so a 503 with
    a structured JSON body should raise ``ScufrisServerError`` with
    the body's detail — not an SSE parse error.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "error": "default model missing",
                "error_type": "DefaultModelMissing",
            },
        )

    client = ScufrisClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    )

    async def go() -> None:
        async for _ in client.chat_stream("cli", "u1", "build", "hi"):
            pass

    with pytest.raises(ScufrisServerError, match="503: default model missing"):
        _run(go())
    _run(client.aclose())


def test_chat_stream_pre_stream_connect_error() -> None:
    """Transport raises ``ConnectError`` before any HTTP response.

    Should become ``ScufrisConnectionError``.
    """
    client = ScufrisClient(base_url="http://test", transport=_FailingTransport())

    async def go() -> None:
        async for _ in client.chat_stream("cli", "u1", "build", "hi"):
            pass

    with pytest.raises(ScufrisConnectionError, match="could not connect"):
        _run(go())


def test_chat_stream_keepalive_comments_dropped() -> None:
    """Server intersperses ``: keepalive`` comment lines.

    They should be silently ignored — no extra events in the output.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        # Mix of SSE events and comment lines
        content = b": keepalive\n\n"
        content += b'event: thinking\ndata: {"kind":"text","source":"scufris","text":"Hello","depth":0}\n\n'
        content += b": keepalive\n\n"
        content += b": keepalive\n\n"
        content += b'event: done\ndata: {"message":"Hello","oc_session_id":"s1","oc_message_id":"m1"}\n\n'
        return httpx.Response(
            200,
            stream=_AsyncByteStream([content]),
            headers={"Content-Type": "text/event-stream"},
        )

    client = ScufrisClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    )

    async def go() -> list[dict[str, object]]:
        evts = []
        async for ev in client.chat_stream("cli", "u1", "build", "hi"):
            evts.append(ev.__dict__)
        return evts

    evts: list[Any] = _run(go())  # type: ignore[assignment]
    assert len(evts) == 2
    assert evts[0]["kind"] == "thinking"
    assert evts[1]["kind"] == "done"
    _run(client.aclose())


def test_chat_stream_unknown_event_synthesized() -> None:
    """Server emits an unexpected event name.

    ``_dispatch`` returns a synthetic ``StreamEvent(kind="error\")``
    with ``error_type="UnknownSSEEvent"``.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_AsyncByteStream(
                [
                    b'event: thinking\ndata: {"kind":"text","source":"scufris","text":"X","depth":0}\n\n',
                    b'event: some_obsolete_v1_event\ndata: {"stuff": true}\n\n',
                    b'event: done\ndata: {"message":"ok","oc_session_id":"s","oc_message_id":"m"}\n\n',
                ]
            ),
            headers={"Content-Type": "text/event-stream"},
        )

    client = ScufrisClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    )

    async def go() -> list[dict[str, object]]:
        evts = []
        async for ev in client.chat_stream("cli", "u1", "build", "hi"):
            evts.append(ev.__dict__)
        return evts

    evts: list[Any] = _run(go())  # type: ignore[assignment]
    assert len(evts) == 3
    assert evts[0]["kind"] == "thinking"
    # The unknown event is synthesized as a StreamEvent(kind="error")
    assert evts[1]["kind"] == "error"
    assert evts[1]["error_type"] == "UnknownSSEEvent"
    assert evts[2]["kind"] == "done"
    _run(client.aclose())


def test_chat_stream_malformed_json() -> None:
    """SSE event with unparseable JSON data raises ``ScufrisServerError``."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_AsyncByteStream(
                [
                    b"event: thinking\ndata: {this is not json\n\n",
                ]
            ),
            headers={"Content-Type": "text/event-stream"},
        )

    client = ScufrisClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    )

    async def go() -> None:
        async for _ in client.chat_stream("cli", "u1", "build", "hi"):
            pass

    with pytest.raises(ScufrisServerError, match="malformed SSE payload"):
        _run(go())
    _run(client.aclose())


def test_chat_stream_missing_thinking_field() -> None:
    """A ``thinking`` event missing ``kind`` raises ``ScufrisServerError``."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_AsyncByteStream(
                [
                    b'event: thinking\ndata: {"source":"scufris","text":"no kind"}\n\n',
                ]
            ),
            headers={"Content-Type": "text/event-stream"},
        )

    client = ScufrisClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    )

    async def go() -> None:
        async for _ in client.chat_stream("cli", "u1", "build", "hi"):
            pass

    with pytest.raises(ScufrisServerError, match="missing required field"):
        _run(go())
    _run(client.aclose())


# ---------------------------------------------------------------------------
# /v1/sessions
# ---------------------------------------------------------------------------


def test_sessions_empty() -> None:
    """Server returns ``200 []``.

    Should produce an empty list, not a 404.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("user_id") == "42"
        return httpx.Response(200, json=[])

    client = _make_client(handler)

    async def go() -> list[Any]:
        return await client.sessions(42)

    result = _run(go())
    assert result == []
    _run(client.aclose())


def test_sessions_populated() -> None:
    """Server returns a list of channel objects.

    Verifies list length and presence of key fields.
    """
    expected = [
        {
            "channel_id": 1,
            "channel": {"surface": "cli", "surface_id": "alex", "agent": "build"},
            "oc_session_id": "ses_abc",
            "last_used_at": 1700000000,
            "title": "Test session",
            "tokens": {"input": 100, "output": 50},
            "cost": 0.05,
        },
        {
            "channel_id": 2,
            "channel": {"surface": "cli", "surface_id": "alex", "agent": "plan"},
            "oc_session_id": "ses_def",
            "last_used_at": 1700000100,
            "title": None,
            "tokens": None,
            "cost": None,
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("user_id") == "7"
        return httpx.Response(200, json=expected)

    client = _make_client(handler)

    async def go() -> list[Any]:
        return await client.sessions(7)

    result: list[Any] = _run(go())  # type: ignore[assignment]
    assert len(result) == 2
    assert result[0]["channel_id"] == 1
    assert result[0]["title"] == "Test session"
    assert result[1]["channel_id"] == 2
    assert result[1]["title"] is None
    _run(client.aclose())


def test_sessions_user_id_query_param() -> None:
    """The ``GET /v1/sessions`` request includes ``?user_id=N``."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "user_id" in request.url.params
        assert request.url.params.get_list("user_id") == ["123"]
        return httpx.Response(200, json=[])

    client = _make_client(handler)

    _run(client.sessions(123))
    _run(client.aclose())


def test_sessions_non_list_rejection() -> None:
    """Server returns a JSON object instead of an array — SDK rejects it."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"})

    client = _make_client(handler)

    async def go() -> None:
        await client.sessions(1)

    with pytest.raises(ScufrisServerError, match="expected JSON array"):
        _run(go())
    _run(client.aclose())


# ---------------------------------------------------------------------------
# /v1/sessions/{id}/clear
# ---------------------------------------------------------------------------


def test_clear_session_cleared_true() -> None:
    """Server returns ``200 {"cleared": true}``.

    The SDK passes the body through.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"cleared": True})

    client = _make_client(handler)

    async def go() -> dict:
        return await client.clear_session(99)

    result = _run(go())
    assert result == {"cleared": True}
    _run(client.aclose())


def test_clear_session_cleared_false() -> None:
    """Server returns ``200 {"cleared": false}``.

    Both true and false are 200 — the SDK doesn't treat false as an
    error.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"cleared": False})

    client = _make_client(handler)

    async def go() -> dict:
        return await client.clear_session(99)

    result = _run(go())
    assert result == {"cleared": False}
    _run(client.aclose())


# ---------------------------------------------------------------------------
# /v1/clear
# ---------------------------------------------------------------------------


def test_clear_count_response() -> None:
    """Server returns ``200 {"count": 3}``.

    Verifies the count value is passed through.
    """
    sent_body: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent_body["_"] = _json.loads(request.content.decode())
        return httpx.Response(200, json={"count": 3})

    client = _make_client(handler)

    async def go() -> dict:
        return await client.clear(user_id=5)

    result = _run(go())
    assert result == {"count": 3}
    assert sent_body["_"]["user_id"] == 5
    _run(client.aclose())


def test_clear_zero_count_is_success() -> None:
    """Server returns ``200 {"count": 0}``.

    Zero-clear is a success (user existed but had no links), not an
    error.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 0})

    client = _make_client(handler)

    async def go() -> dict:
        return await client.clear(user_id=10)

    result = _run(go())
    assert result == {"count": 0}
    _run(client.aclose())


def test_clear_request_body_shape() -> None:
    """The POST /v1/clear request body is ``{"user_id": N}``.

    Verifies the exact wire shape — no extra fields, no nested
    structure.
    """
    body_received: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body_received["_"] = _json.loads(request.content.decode())
        return httpx.Response(200, json={"count": 1})

    client = _make_client(handler)

    _run(client.clear(user_id=999))
    assert body_received["_"] == {"user_id": 999}
    _run(client.aclose())


# ---------------------------------------------------------------------------
# SSE parsing edge cases (direct _parse_sse_stream)
# ---------------------------------------------------------------------------


def test_parse_sse_multiline_data() -> None:
    """Data spans multiple ``data:`` lines, joined with ``\\n``.

    The SSE spec says multi-line ``data:`` blocks are joined with
    newlines.  The SDK's ``_parse_sse_stream`` accumulates into
    ``data_buf`` and joins with ``\\n``.

    We verify the join by dispatching a ``done`` event whose
    ``message`` key and value are on separate data lines.  The
    joined payload is valid JSON — the newline lands between the
    colon and the string (whitespace is allowed per the JSON spec).
    """

    async def go() -> list[Any]:
        parser = _AsyncStrIter(
            [
                "event: done",
                'data: {"message":',
                'data:  "hello world","oc_session_id":"s","oc_message_id":"m"}',
                "",
            ]
        )
        evts = []
        async for ev in _parse_sse_stream(parser):
            evts.append(ev)
        return evts

    evts: list[Any] = _run(go())  # type: ignore[assignment]
    assert len(evts) == 1
    assert evts[0].kind == "done"
    assert evts[0].text == "hello world"


def test_parse_sse_trailing_event_no_final_blank() -> None:
    """Last event has no trailing blank line — still dispatched.

    Defensive: the server normally sends the trailing blank, but
    the parser should handle the missing one.
    """

    async def go() -> list[Any]:
        parser = _AsyncStrIter(
            [
                "event: thinking",
                'data: {"kind":"text","source":"scufris","text":"x","depth":0}',
                "",
                "event: done",
                'data: {"message":"ok","oc_session_id":"s","oc_message_id":"m"}',
                # No final blank line!
            ]
        )
        evts = []
        async for ev in _parse_sse_stream(parser):
            evts.append(ev)
        return evts

    evts: list[Any] = _run(go())  # type: ignore[assignment]
    assert len(evts) == 2
    assert evts[0].kind == "thinking"
    assert evts[1].kind == "done"
    assert evts[1].text == "ok"


def test_parse_sse_unknown_fields_dropped() -> None:
    """Lines with ``id:``, ``retry:`` etc. are dropped per SSE spec."""

    async def go() -> list[Any]:
        parser = _AsyncStrIter(
            [
                "id: event-42",
                "retry: 3000",
                "event: thinking",
                'data: {"kind":"text","source":"scufris","text":"ok","depth":0}',
                "custom_field: ignored",
                "",
            ]
        )
        evts = []
        async for ev in _parse_sse_stream(parser):
            evts.append(ev)
        return evts

    evts: list[Any] = _run(go())  # type: ignore[assignment]
    assert len(evts) == 1
    assert evts[0].kind == "thinking"
    assert evts[0].thinking.text == "ok"


class _AsyncStrIter:
    """An ``AsyncIterator[str]`` backed by a list of strings."""

    def __init__(self, items: list[str]) -> None:
        self._items = items
        self._i = 0

    def __aiter__(self) -> _AsyncStrIter:
        self._i = 0
        return self

    async def __anext__(self) -> str:
        if self._i >= len(self._items):
            raise StopAsyncIteration
        val = self._items[self._i]
        self._i += 1
        return val

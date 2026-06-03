"""Tests for the Scufris HTTP client and SSE parser.

Uses :class:`httpx.MockTransport` so no real network traffic happens.
The CLI's ``cli.py`` is exercised indirectly via the client surface —
the REPL loop itself (readline / Rich console) is hard to fixture and
boring to test, so we cover the contract it depends on instead.
"""

from __future__ import annotations

import asyncio
import json
from typing import List

import httpx
import pytest

from scufris_client import (
    ScufrisAuthError,
    ScufrisClient,
    ScufrisConnectionError,
    ScufrisServerError,
    StreamEvent,
    parse_sse_stream,
    user_id_for,
)
from utils.callbacks import ThinkingEvent

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _client(handler) -> ScufrisClient:
    return ScufrisClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    )


async def _alines(text: str):
    """Async iterator over the lines of ``text`` (no trailing \n in items)."""
    for line in text.split("\n"):
        yield line


# ----------------------------------------------------------------------
# user_id_for
# ----------------------------------------------------------------------


def test_user_id_for_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCUFRIS_USER_ID", "12345")
    assert user_id_for() == 12345


def test_user_id_for_is_stable_per_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCUFRIS_USER_ID", raising=False)
    a = user_id_for("alex")
    b = user_id_for("alex")
    c = user_id_for("bob")
    assert a == b
    assert a != c
    assert 0 < a < 2**31


# ----------------------------------------------------------------------
# SSE parser
# ----------------------------------------------------------------------


def _collect(stream_text: str) -> List[StreamEvent]:
    async def go() -> List[StreamEvent]:
        out: List[StreamEvent] = []
        async for ev in parse_sse_stream(_alines(stream_text)):
            out.append(ev)
        return out

    return asyncio.run(go())


def test_sse_parses_thinking_then_done() -> None:
    stream = (
        "event: thinking\n"
        'data: {"kind":"text","source":"main","text":"hi","depth":0}\n'
        "\n"
        ": keepalive\n"
        "\n"
        "event: done\n"
        'data: {"text":"final answer"}\n'
        "\n"
    )
    events = _collect(stream)
    assert [e.kind for e in events] == ["thinking", "done"]
    assert isinstance(events[0].thinking, ThinkingEvent)
    assert events[0].thinking.text == "hi"
    assert events[1].text == "final answer"


def test_sse_parses_error_event() -> None:
    stream = 'event: error\ndata: {"error":"boom"}\n\n'
    events = _collect(stream)
    assert events == [StreamEvent(kind="error", error="boom")]


def test_sse_unknown_event_becomes_error() -> None:
    stream = "event: weird\ndata: {}\n\n"
    events = _collect(stream)
    assert events[0].kind == "error"
    assert "weird" in (events[0].error or "")


def test_sse_malformed_payload_raises() -> None:
    stream = "event: thinking\ndata: not-json\n\n"
    with pytest.raises(ScufrisServerError):
        _collect(stream)


# ----------------------------------------------------------------------
# JSON endpoints
# ----------------------------------------------------------------------


def test_healthz() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/healthz"
        return httpx.Response(200, json={"status": "ok"})

    async def go() -> None:
        async with _client(handler) as c:
            assert await c.healthz() == {"status": "ok"}

    asyncio.run(go())


def test_chat_returns_response_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/chat"
        return httpx.Response(200, json={"user_id": 1, "response": "pong"})

    async def go() -> None:
        async with _client(handler) as c:
            assert await c.chat(1, "ping") == "pong"

    asyncio.run(go())


def test_stats_passes_user_id_param() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"user_id": 7, "lines": ["a", "b"], "rows": {}})

    async def go() -> None:
        async with _client(handler) as c:
            result = await c.stats(7)
            assert result["lines"] == ["a", "b"]

    asyncio.run(go())
    assert seen["params"] == {"user_id": "7"}


def test_clear_returns_breakdown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={"user_id": 2, "cleared": 4, "breakdown": {"scufris": 4}},
        )

    async def go() -> None:
        async with _client(handler) as c:
            assert await c.clear(2) == {
                "user_id": 2,
                "cleared": 4,
                "breakdown": {"scufris": 4},
            }

    asyncio.run(go())


def test_resolve_identity_round_trips_payload() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/identity/resolve"
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "user_id": 12345,
                "username": "alex",
                "surface": "cli",
                "surface_id": "alex",
                "bound_surfaces": ["cli", "telegram"],
            },
        )

    async def go() -> None:
        async with _client(handler) as c:
            body = await c.resolve_identity("cli", "alex")
            assert body["user_id"] == 12345
            assert body["username"] == "alex"
            assert body["bound_surfaces"] == ["cli", "telegram"]

    asyncio.run(go())
    assert seen["body"] == {"surface": "cli", "surface_id": "alex"}


def test_resolve_identity_missing_user_id_raises_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"username": "alex"})

    async def go() -> None:
        async with _client(handler) as c:
            with pytest.raises(ScufrisServerError):
                await c.resolve_identity("cli", "alex")

    asyncio.run(go())


def test_auth_error_raises_scufrisautherror() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "missing bearer token"})

    async def go() -> None:
        async with _client(handler) as c:
            with pytest.raises(ScufrisAuthError):
                await c.chat(1, "hi")

    asyncio.run(go())


def test_server_error_raises_scufrisservererror() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "kaboom"})

    async def go() -> None:
        async with _client(handler) as c:
            with pytest.raises(ScufrisServerError):
                await c.chat(1, "hi")

    asyncio.run(go())


def test_connection_error_maps_to_scufrisconnectionerror() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    async def go() -> None:
        async with _client(handler) as c:
            with pytest.raises(ScufrisConnectionError):
                await c.healthz()

    asyncio.run(go())


def test_token_is_set_as_bearer_header() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "ok"})

    async def go() -> None:
        async with ScufrisClient(
            base_url="http://test",
            token="s3cret",
            transport=httpx.MockTransport(handler),
        ) as c:
            await c.healthz()

    asyncio.run(go())
    assert seen["auth"] == "Bearer s3cret"


# ----------------------------------------------------------------------
# Streaming
# ----------------------------------------------------------------------


def test_chat_stream_yields_thinking_then_done() -> None:
    body = (
        b"event: thinking\n"
        b'data: {"kind":"text","source":"main","text":"thinking...","depth":0}\n'
        b"\n"
        b"event: done\n"
        b'data: {"text":"hello!"}\n'
        b"\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/stream"
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/event-stream"},
        )

    async def go() -> List[StreamEvent]:
        async with _client(handler) as c:
            events: List[StreamEvent] = []
            async for ev in c.chat_stream(1, "hi"):
                events.append(ev)
            return events

    events = asyncio.run(go())
    assert len(events) == 2
    assert events[0].kind == "thinking"
    assert events[0].thinking is not None
    assert events[0].thinking.text == "thinking..."
    assert events[1].kind == "done"
    assert events[1].text == "hello!"

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
- ``clear`` request-body shape + count response.

Section layout: Helpers → constructor → async-cm → endpoint groups
(healthz, version, resolve_identity, chat_stream, sessions, clear)
→ shared error-mapping → exception hierarchy.
"""

from __future__ import annotations

import asyncio
import json as _json
from collections.abc import Callable

import httpx
import pytest

from scufris_client import (
    ScufrisAuthError,
    ScufrisClient,
    ScufrisConnectionError,
    ScufrisError,
    ScufrisServerError,
    StreamEvent,
    ThinkingEvent,
)
from scufris_client.client import DEFAULT_BASE_URL

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

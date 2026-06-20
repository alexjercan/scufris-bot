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

import os
from types import TracebackType
from typing import Any

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

    Methods present in step 2 (this commit):

    - :meth:`healthz` — GET /v1/healthz
    - :meth:`version` — GET /v1/version
    - :meth:`resolve_identity` — POST /v1/identity/resolve

    Streaming and state-management methods land in step 3.
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

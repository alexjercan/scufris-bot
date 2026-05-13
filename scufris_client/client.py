"""HTTP client + SSE parser for talking to ``scufris-server``.

The wire shape is locked by ``scufris_server``:

  * ``POST /v1/chat`` → ``{user_id, response}``
  * ``POST /v1/chat/stream`` → SSE; ``thinking`` events carry a JSON
    payload with the same field set as :class:`utils.callbacks.ThinkingEvent`,
    plus a single terminal ``done`` (``{text}``) or ``error`` (``{error}``).
  * ``GET /v1/stats?user_id=`` → ``{lines, rows, user_id}``.
  * ``POST /v1/clear`` → ``{user_id, cleared, breakdown}``.
  * ``GET /v1/healthz`` → ``{status: "ok"}``.

We keep the SSE parser hand-rolled (~30 LoC) so we don't pull in
``httpx-sse`` for one shape of stream.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from utils.callbacks import ThinkingEvent

DEFAULT_BASE_URL = "http://127.0.0.1:8765"
# Mirrors scufris_server.sse.KEEPALIVE_SECONDS; SSE reads can idle for
# this long. We pad generously so transient slowness doesn't drop the
# stream.
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------


class ScufrisError(RuntimeError):
    """Base class for client-side errors. The CLI catches this."""


class ScufrisConnectionError(ScufrisError):
    """The server was unreachable (refused, DNS, timeout)."""


class ScufrisAuthError(ScufrisError):
    """401/403 from the server."""


class ScufrisServerError(ScufrisError):
    """Any other non-2xx response, or a malformed body."""


# ----------------------------------------------------------------------
# Data shapes
# ----------------------------------------------------------------------


@dataclass
class StreamEvent:
    """One event yielded by :meth:`ScufrisClient.chat_stream`.

    Either ``thinking`` carries a :class:`ThinkingEvent`, or one of the
    terminal kinds (``done`` / ``error``) carries text. Exactly one
    terminal event is produced per stream.
    """

    kind: str  # "thinking" | "done" | "error"
    thinking: Optional[ThinkingEvent] = None
    text: Optional[str] = None  # for done
    error: Optional[str] = None  # for error


def _thinking_from_payload(data: Dict[str, Any]) -> ThinkingEvent:
    """Reconstruct a ThinkingEvent from the server's JSON payload.

    The server only includes optional fields when set, so we fall back
    to ``None`` for anything missing rather than failing the parse.
    """
    return ThinkingEvent(
        kind=data["kind"],
        source=data["source"],
        text=data["text"],
        depth=int(data.get("depth", 0)),
        arg=data.get("arg"),
        context=data.get("context"),
        prior_turns=data.get("prior_turns"),
        evicted=data.get("evicted"),
        new_facts=data.get("new_facts"),
    )


# ----------------------------------------------------------------------
# SSE parser
# ----------------------------------------------------------------------


async def parse_sse_stream(
    lines: AsyncIterator[str],
) -> AsyncIterator[StreamEvent]:
    """Parse an SSE byte/line stream into :class:`StreamEvent`s.

    Accepts an async iterator of *decoded* lines (no trailing newline).
    Implements the subset of the SSE spec we actually use: ``event:``,
    ``data:``, blank-line dispatch, and comment lines (starting with
    ``:``) which we drop. Multi-line ``data:`` is concatenated with
    newlines per spec.
    """
    event_name = ""
    data_buf: List[str] = []

    async for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            # Dispatch.
            if event_name or data_buf:
                payload_text = "\n".join(data_buf)
                yield _dispatch(event_name, payload_text)
            event_name = ""
            data_buf = []
            continue
        if line.startswith(":"):
            # Comment / keepalive.
            continue
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
            continue
        if line.startswith("data:"):
            data_buf.append(line[len("data:") :].lstrip(" "))
            continue
        # Unknown field — ignore per spec.

    # Trailing event without final blank line (rare, but be lenient).
    if event_name or data_buf:
        yield _dispatch(event_name, "\n".join(data_buf))


def _dispatch(event_name: str, payload_text: str) -> StreamEvent:
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except json.JSONDecodeError as exc:
        raise ScufrisServerError(
            f"malformed SSE payload for event {event_name!r}: {exc}"
        ) from exc

    if event_name == "thinking":
        return StreamEvent(kind="thinking", thinking=_thinking_from_payload(payload))
    if event_name == "done":
        return StreamEvent(kind="done", text=payload.get("text", ""))
    if event_name == "error":
        return StreamEvent(kind="error", error=payload.get("error", "unknown"))
    # Unknown event — surface as error so the caller doesn't hang.
    return StreamEvent(kind="error", error=f"unexpected SSE event: {event_name!r}")


# ----------------------------------------------------------------------
# Identity
# ----------------------------------------------------------------------


def user_id_for(name: Optional[str] = None) -> int:
    """Resolve an integer user id for the current CLI session.

    Resolution order:

    1. ``SCUFRIS_USER_ID`` env var (explicit integer override).
    2. The given ``name`` argument, hashed to a stable positive int.
    3. ``SCUFRIS_USER`` env var, hashed.
    4. ``getpass.getuser()``, hashed.

    The hash is the low 31 bits of MD5 — stable across processes and
    well below the 2**63 sqlite ``INTEGER`` limit for any future
    persistence layer.
    """
    explicit = os.environ.get("SCUFRIS_USER_ID")
    if explicit:
        try:
            return int(explicit)
        except ValueError as exc:
            raise ScufrisError(
                f"SCUFRIS_USER_ID must be an integer, got {explicit!r}"
            ) from exc

    candidate = name or os.environ.get("SCUFRIS_USER") or getpass.getuser()
    digest = hashlib.md5(candidate.encode("utf-8")).digest()
    # Take low 4 bytes, mask to 31 bits → fits Python int / sqlite int / JSON number.
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


# ----------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------


class ScufrisClient:
    """Async HTTP client for the Scufris server.

    Owns an :class:`httpx.AsyncClient` for connection reuse. Use as an
    async context manager so the underlying pool gets closed cleanly,
    or call :meth:`aclose` manually.

    The constructor accepts a ``transport`` injection point so tests
    can wire in :class:`httpx.MockTransport` without monkey-patching.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: Optional[str] = None,
        *,
        timeout: Optional[httpx.Timeout] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        headers: Dict[str, str] = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout or DEFAULT_TIMEOUT,
            transport=transport,
        )

    async def __aenter__(self) -> "ScufrisClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def healthz(self) -> Dict[str, Any]:
        return await self._json("GET", "/v1/healthz")

    async def stats(self, user_id: int) -> Dict[str, Any]:
        return await self._json("GET", "/v1/stats", params={"user_id": user_id})

    async def clear(self, user_id: int) -> Dict[str, Any]:
        return await self._json("POST", "/v1/clear", json={"user_id": user_id})

    async def chat(self, user_id: int, message: str) -> str:
        body = await self._json(
            "POST", "/v1/chat", json={"user_id": user_id, "message": message}
        )
        text = body.get("response")
        if not isinstance(text, str):
            raise ScufrisServerError(f"missing 'response' in chat reply: {body!r}")
        return text

    async def chat_stream(
        self, user_id: int, message: str
    ) -> AsyncIterator[StreamEvent]:
        """Yield :class:`StreamEvent`s for one streaming chat turn.

        The async generator is responsible for closing the underlying
        SSE connection on exit (including when the caller stops
        iterating early — e.g. on Ctrl-C). Server-side, the daemon
        cancels the in-flight task when the client disconnects.
        """
        try:
            req = self._client.build_request(
                "POST",
                "/v1/chat/stream",
                json={"user_id": user_id, "message": message},
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
            self._raise_for_status(response)

            async def _lines() -> AsyncIterator[str]:
                # httpx's aiter_lines yields strings split on \n, with
                # \r stripped — exactly what the SSE parser wants.
                async for line in response.aiter_lines():
                    yield line

            async for event in parse_sse_stream(_lines()):
                yield event
        finally:
            await response.aclose()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
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
            raise ScufrisServerError(f"non-JSON response: {exc}") from exc
        if not isinstance(data, dict):
            raise ScufrisServerError(f"expected JSON object, got {type(data).__name__}")
        return data

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        # Pull a readable detail; ignore failure to read body.
        detail: str
        try:
            payload = response.json()
            detail = payload.get("detail") or payload.get("error") or str(payload)
        except Exception:  # noqa: BLE001
            detail = response.text or response.reason_phrase or ""
        if response.status_code in (401, 403):
            raise ScufrisAuthError(f"{response.status_code}: {detail}")
        raise ScufrisServerError(f"{response.status_code}: {detail}")

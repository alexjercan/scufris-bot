"""Structured logging + per-request ID tracking (step 6 of #9).

This is a *placeholder* logging layer: enough to make every line
machine-parseable JSON, enough to thread a request-ID through every
log call within a request, and nothing more. The full observability
story (structlog, /metrics, sampling, OpenTelemetry) lives in #28.

Pieces
------
- :data:`REQUEST_ID_VAR` — a :class:`contextvars.ContextVar` carrying
  the current request's ID (or ``None`` outside a request).
- :func:`generate_request_id` — returns a 26-char ULID string.
- :class:`JsonFormatter` — flat one-line JSON with ``ts/level/logger/
  request_id/message`` and any user-supplied ``extra=`` kwargs lifted
  to top-level fields.
- :func:`setup_logging` — idempotent installer; wires a stdout
  handler + JSON formatter onto the ``scufris_server`` logger.
- :class:`RequestIdMiddleware` — pure-ASGI middleware that mints (or
  accepts) an ``X-Request-Id``, sets the contextvar and
  ``request.state.request_id``, and echoes the ID in the response
  header.

Why pure ASGI for the middleware
--------------------------------
``BaseHTTPMiddleware`` buffers full response bodies, which breaks SSE
(coming in #11) and adds latency for nothing. Pure ASGI is ~25 lines
and avoids those traps.

Why ULIDs
---------
- 26 chars, lexicographically sortable by timestamp, base32-encoded
  (no ambiguous chars), 80 bits of randomness.
- Validatable: malformed incoming ``X-Request-Id`` headers (log
  injection attempts) are rejected and replaced with a fresh ULID.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

import ulid

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, MutableMapping

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# Public: importers call ``REQUEST_ID_VAR.get()`` to thread the current
# request's id into log records, metrics, downstream HTTP calls, etc.
REQUEST_ID_VAR: ContextVar[str | None] = ContextVar("scufris_request_id", default=None)

_HEADER_NAME = b"x-request-id"

# LogRecord attributes the stdlib sets automatically. We strip these
# when lifting "extras" so the JSON payload stays tidy.
_RESERVED_LOGRECORD_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
        "taskName",
    }
)


def generate_request_id() -> str:
    """Return a fresh 26-char ULID string (Crockford base32)."""
    return str(ulid.ULID())


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """One JSON object per log line, flat schema.

    Always present: ``ts`` (RFC3339 UTC, ms precision), ``level``,
    ``logger``, ``request_id``, ``message``. Any user-supplied
    ``extra={...}`` is lifted into top-level keys. Exceptions are
    rendered via :meth:`logging.Formatter.formatException` and
    attached as ``exc_info``.

    Non-JSON-serialisable values fall through to ``str(value)``.
    """

    _BASE_KEYS: ClassVar[tuple[str, ...]] = (
        "ts",
        "level",
        "logger",
        "request_id",
        "message",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self._format_ts(record.created),
            "level": record.levelname,
            "logger": record.name,
            "request_id": REQUEST_ID_VAR.get(),
            "message": record.getMessage(),
        }
        # Lift caller-supplied extras (``logger.info("x", extra={...})``).
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOGRECORD_KEYS or key.startswith("_"):
                continue
            if key in payload:  # don't let an extra shadow a base key
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)

    @staticmethod
    def _format_ts(created: float) -> str:
        """Convert a ``time.time()``-style float to RFC3339 UTC with ms."""
        dt = datetime.fromtimestamp(created, tz=UTC)
        # isoformat with ms precision yields '...+00:00'; canonicalise to 'Z'.
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

_configured = False

# Loggers we attach our JSON handler to. Anything else (third-party
# libs, root) is left at its defaults so embedders can layer their own
# logging on top without us hijacking it.
_OWNED_LOGGERS: tuple[str, ...] = (
    "scufris_server",
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
)


def setup_logging(level: int = logging.INFO) -> None:
    """Install the JSON handler on scufris-server's owned loggers.

    Idempotent — safe to call multiple times. Configures only the
    loggers in :data:`_OWNED_LOGGERS` (scufris's own + uvicorn's
    three) so embedders and test runners don't get root hijacked.
    Propagation is disabled on owned loggers so messages don't
    double-log if something later attaches a handler to root.

    Tests do not call this; they rely on pytest's ``caplog`` (which
    attaches to a named logger directly via ``caplog.at_level(level,
    logger=...)`` and so works regardless of propagation).

    Production callers (``__main__``) should pair this with
    ``uvicorn.run(..., log_config=None)`` so uvicorn doesn't replace
    our handlers with its default text formatter.
    """
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.setLevel(level)
    for name in _OWNED_LOGGERS:
        logger = logging.getLogger(name)
        for existing in list(logger.handlers):
            logger.removeHandler(existing)
        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
    _configured = True


def _reset_for_tests() -> None:
    """Drop the configured flag + handlers from all owned loggers."""
    global _configured
    _configured = False
    for name in _OWNED_LOGGERS:
        logger = logging.getLogger(name)
        for existing in list(logger.handlers):
            logger.removeHandler(existing)
        logger.propagate = True


# ---------------------------------------------------------------------------
# ASGI middleware
# ---------------------------------------------------------------------------


class RequestIdMiddleware:
    """Mint or accept ``X-Request-Id``; expose it; echo it.

    Pipeline per HTTP request:

    1. If the incoming request has a valid ULID in ``X-Request-Id``,
       use it; otherwise generate a fresh one.
    2. Set :data:`REQUEST_ID_VAR` for the lifetime of the request so
       log records inside handlers automatically carry the id.
    3. Stash the id under ``scope["state"]["request_id"]`` so
       handlers can read ``request.state.request_id``.
    4. On the response, add an ``X-Request-Id`` header (unless the
       handler already set one).
    5. Reset the contextvar on exit so it can't leak into the next
       request scheduled on the same task / worker.

    Non-HTTP scopes (``lifespan``, ``websocket``) pass through
    untouched.
    """

    def __init__(self, app: "ASGIApp") -> None:
        self.app = app

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = self._extract_header(scope.get("headers") or [])
        request_id = self._validate(incoming) or generate_request_id()

        # Ensure scope.state exists, then expose the id to handlers.
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        token = REQUEST_ID_VAR.set(request_id)
        try:
            await self.app(scope, receive, self._wrap_send(send, request_id))
        finally:
            REQUEST_ID_VAR.reset(token)

    @staticmethod
    def _extract_header(headers: "Iterable[tuple[bytes, bytes]]") -> str | None:
        for key, value in headers:
            if key.lower() == _HEADER_NAME:
                # ASGI headers are bytes; X-Request-Id is ASCII-only by
                # convention. latin-1 decode is a safe superset.
                return value.decode("latin-1", errors="replace").strip()
        return None

    @staticmethod
    def _validate(candidate: str | None) -> str | None:
        if not candidate:
            return None
        try:
            ulid.ULID.from_str(candidate)
        except (ValueError, TypeError):
            return None
        return candidate

    @staticmethod
    def _wrap_send(send: "Send", request_id: str) -> "Send":
        rid_bytes = request_id.encode("ascii")

        async def wrapped(message: "Message") -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                # Don't clobber a handler that set its own id deliberately.
                if not any(k.lower() == _HEADER_NAME for k, _ in headers):
                    headers.append((_HEADER_NAME, rid_bytes))
                message["headers"] = headers
            await send(message)

        return wrapped

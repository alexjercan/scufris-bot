"""Tests for ``scufris_server.logging`` (step 6 of #9)."""

from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from scufris_server.logging import (
    REQUEST_ID_VAR,
    JsonFormatter,
    RequestIdMiddleware,
    _reset_for_tests,
    generate_request_id,
    setup_logging,
)

# Crockford base32 — the alphabet ULIDs use.
_CROCKFORD = frozenset("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Iterator[None]:
    """Clear logger handlers and the contextvar between tests."""
    _reset_for_tests()
    token = REQUEST_ID_VAR.set(None)
    try:
        yield
    finally:
        REQUEST_ID_VAR.reset(token)
        _reset_for_tests()


# ---------------------------------------------------------------------------
# generate_request_id
# ---------------------------------------------------------------------------


def test_generate_request_id_is_26_char_crockford() -> None:
    rid = generate_request_id()
    assert len(rid) == 26
    assert set(rid) <= _CROCKFORD, f"non-Crockford chars: {set(rid) - _CROCKFORD}"


def test_generate_request_id_returns_distinct_values() -> None:
    ids = {generate_request_id() for _ in range(100)}
    # ULIDs share a millisecond timestamp prefix when generated rapidly,
    # but the random suffix should keep all 100 unique.
    assert len(ids) == 100


# ---------------------------------------------------------------------------
# JsonFormatter
# ---------------------------------------------------------------------------


def _make_record(
    *,
    name: str = "scufris_server.test",
    level: int = logging.INFO,
    msg: str = "hello %s",
    args: tuple[Any, ...] = ("world",),
    exc_info: Any = None,
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname="/fake/path.py",
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )


def test_json_formatter_emits_required_keys() -> None:
    out = json.loads(JsonFormatter().format(_make_record()))
    assert set(out) >= {"ts", "level", "logger", "request_id", "message"}
    assert out["level"] == "INFO"
    assert out["logger"] == "scufris_server.test"
    assert out["message"] == "hello world"
    assert out["request_id"] is None


def test_json_formatter_ts_is_rfc3339_utc_with_ms_and_z() -> None:
    payload = json.loads(JsonFormatter().format(_make_record()))
    ts = payload["ts"]
    # YYYY-MM-DDTHH:MM:SS.mmmZ — 24 chars exactly.
    assert ts.endswith("Z")
    assert ts[10] == "T"
    assert ts[19] == "."
    assert len(ts) == 24


def test_json_formatter_includes_request_id_from_contextvar() -> None:
    token = REQUEST_ID_VAR.set("01J0AB7E0KCSV0K5AYY1X14J9G")
    try:
        out = json.loads(JsonFormatter().format(_make_record()))
    finally:
        REQUEST_ID_VAR.reset(token)
    assert out["request_id"] == "01J0AB7E0KCSV0K5AYY1X14J9G"


def test_json_formatter_lifts_extras_to_top_level() -> None:
    record = _make_record()
    record.user_id = 42  # type: ignore[attr-defined]
    record.session = "abc"  # type: ignore[attr-defined]
    out = json.loads(JsonFormatter().format(record))
    assert out["user_id"] == 42
    assert out["session"] == "abc"


def test_json_formatter_does_not_let_extras_shadow_base_keys() -> None:
    record = _make_record()
    record.level = "PRETEND-DEBUG"  # type: ignore[attr-defined]
    record.request_id = "fake-id"  # type: ignore[attr-defined]
    out = json.loads(JsonFormatter().format(record))
    # Base values win.
    assert out["level"] == "INFO"
    assert out["request_id"] is None


def test_json_formatter_renders_exception_info() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = _make_record(level=logging.ERROR, exc_info=sys.exc_info())
    out = json.loads(JsonFormatter().format(record))
    assert "exc_info" in out
    assert "ValueError" in out["exc_info"]
    assert "boom" in out["exc_info"]


def test_json_formatter_handles_non_serialisable_extras() -> None:
    record = _make_record()
    record.path = object()  # type: ignore[attr-defined]
    out = json.loads(JsonFormatter().format(record))
    # default=str catches it; no exception, value becomes the repr string.
    assert isinstance(out["path"], str)


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


def test_setup_logging_is_idempotent() -> None:
    setup_logging()
    setup_logging()
    setup_logging()
    handlers = logging.getLogger("scufris_server").handlers
    assert len(handlers) == 1


def test_setup_logging_installs_json_formatter() -> None:
    setup_logging()
    handlers = logging.getLogger("scufris_server").handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0].formatter, JsonFormatter)


def test_setup_logging_covers_uvicorn_loggers() -> None:
    setup_logging()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        handlers = logging.getLogger(name).handlers
        assert len(handlers) == 1, f"{name} expected 1 handler, got {len(handlers)}"
        assert isinstance(handlers[0].formatter, JsonFormatter)


def test_setup_logging_disables_propagation_on_owned_loggers() -> None:
    setup_logging()
    for name in ("scufris_server", "uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).propagate is False, (
            f"{name} should have propagate=False"
        )


def test_setup_logging_leaves_root_alone() -> None:
    before = list(logging.getLogger().handlers)
    setup_logging()
    after = list(logging.getLogger().handlers)
    assert before == after


# ---------------------------------------------------------------------------
# RequestIdMiddleware (integration via FastAPI TestClient)
# ---------------------------------------------------------------------------


def _make_probe_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/probe")
    def probe(request: Request) -> dict[str, str | None]:
        return {
            "from_state": getattr(request.state, "request_id", None),
            "from_var": REQUEST_ID_VAR.get(),
        }

    return app


def test_middleware_generates_id_when_header_absent() -> None:
    client = TestClient(_make_probe_app())
    resp = client.get("/probe")
    assert resp.status_code == 200

    body = resp.json()
    rid = body["from_state"]
    assert rid is not None
    assert len(rid) == 26
    assert set(rid) <= _CROCKFORD
    assert body["from_var"] == rid
    assert resp.headers["x-request-id"] == rid


def test_middleware_honors_valid_incoming_header() -> None:
    incoming = generate_request_id()
    client = TestClient(_make_probe_app())
    resp = client.get("/probe", headers={"X-Request-Id": incoming})
    assert resp.json()["from_state"] == incoming
    assert resp.headers["x-request-id"] == incoming


def test_middleware_rejects_malformed_incoming_header() -> None:
    client = TestClient(_make_probe_app())
    resp = client.get("/probe", headers={"X-Request-Id": "not-a-ulid"})
    rid = resp.json()["from_state"]
    assert rid != "not-a-ulid"
    assert len(rid) == 26
    assert resp.headers["x-request-id"] == rid


def test_middleware_rejects_empty_incoming_header() -> None:
    client = TestClient(_make_probe_app())
    resp = client.get("/probe", headers={"X-Request-Id": ""})
    rid = resp.json()["from_state"]
    assert rid is not None
    assert len(rid) == 26


def test_middleware_resets_contextvar_after_request() -> None:
    client = TestClient(_make_probe_app())
    REQUEST_ID_VAR.set(None)
    client.get("/probe")
    # After the request returns, the contextvar must be back to None
    # — otherwise an ID would leak into the next request scheduled on
    # the same task.
    assert REQUEST_ID_VAR.get() is None


def test_middleware_does_not_overwrite_handler_supplied_header() -> None:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/custom")
    def custom() -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse({}, headers={"X-Request-Id": "handler-wins"})

    resp = TestClient(app).get("/custom")
    # Middleware respects what the handler set.
    assert resp.headers["x-request-id"] == "handler-wins"


# ---------------------------------------------------------------------------
# End-to-end: log line within a request carries the request id
# ---------------------------------------------------------------------------


def test_log_line_inside_request_contains_request_id() -> None:
    """Wire formatter onto an isolated logger; emit during a request;
    assert the captured line's JSON has the right request_id."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    handler.setLevel(logging.INFO)

    test_logger = logging.getLogger("scufris_server.test_e2e")
    test_logger.handlers.clear()
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.INFO)
    test_logger.propagate = False

    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/log")
    def log_route() -> dict[str, str]:
        test_logger.info("inside handler", extra={"who": "alice"})
        return {"ok": "yes"}

    try:
        resp = TestClient(app).get("/log")
        rid = resp.headers["x-request-id"]

        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["request_id"] == rid
        assert payload["message"] == "inside handler"
        assert payload["who"] == "alice"
        assert payload["level"] == "INFO"
    finally:
        test_logger.removeHandler(handler)

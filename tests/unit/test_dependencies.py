"""Tests for ``scufris_server.dependencies`` (steps 7+8 of #9).

Covers the FastAPI DI helpers that route handlers consume:

- :func:`get_opencode_client` — returns the lifespan-scoped client.
- :func:`get_db_conn` — yields a per-request SQLite connection.

We don't test :func:`get_opencode_client` exhaustively here because
its behaviour is implicitly exercised by every other route test that
uses ``Depends(get_opencode_client)``. The DB-conn dep gets thorough
coverage because step 8's chat handler is its first real consumer.
"""

from __future__ import annotations

import inspect
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

import pytest
import pytest_asyncio
import respx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from scufris_server.app import create_app
from scufris_server.config import Settings
from scufris_server.dependencies import get_db_conn, get_opencode_client

OPENCODE_TEST_URL = "http://opencode.test"
HEALTH_OK_BODY = {"healthy": True, "version": "1.15.13"}
PROVIDER_OK_BODY: dict[str, Any] = {
    "all": [],
    "default": {"ollama": "qwen3:latest"},
    "connected": ["ollama"],
}


def _mock_happy_opencode(mock: respx.MockRouter) -> None:
    mock.get("/global/health").mock(return_value=Response(200, json=HEALTH_OK_BODY))
    mock.get("/provider").mock(return_value=Response(200, json=PROVIDER_OK_BODY))


def _make_app(tmp_path: Path) -> tuple[FastAPI, Settings]:
    settings = Settings(state_dir=tmp_path, opencode_url=OPENCODE_TEST_URL)
    return create_app(settings), settings


# ---------------------------------------------------------------------------
# get_opencode_client
# ---------------------------------------------------------------------------


def test_get_opencode_client_returns_lifespan_client(tmp_path: Path) -> None:
    app, settings = _make_app(tmp_path)
    captured: list[object] = []

    @app.get("/_dep_check_oc")
    def _check(
        client: Annotated[Any, Depends(get_opencode_client)],
    ) -> dict[str, str]:
        captured.append(client)
        return {"ok": "yes"}

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        with TestClient(app) as test_client:
            test_client.get("/_dep_check_oc")
            # Handler saw the same instance the lifespan stashed.
            assert captured == [app.state.opencode]


# ---------------------------------------------------------------------------
# get_db_conn
# ---------------------------------------------------------------------------


def test_get_db_conn_yields_working_connection(tmp_path: Path) -> None:
    """The dep gives the handler a connection that can read the schema."""
    app, settings = _make_app(tmp_path)
    seen_tables: list[set[str]] = []

    @app.get("/_dep_check_db")
    async def _check(
        conn: Annotated[sqlite3.Connection, Depends(get_db_conn)],
    ) -> dict[str, int]:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        seen_tables.append({r["name"] for r in rows})
        return {"count": len(rows)}

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        with TestClient(app) as test_client:
            resp = test_client.get("/_dep_check_db")

    assert resp.status_code == 200
    assert seen_tables, "handler did not run"
    assert "users" in seen_tables[0]
    assert "channels" in seen_tables[0]
    assert "session_links" in seen_tables[0]


def test_get_db_conn_closes_connection_after_request(tmp_path: Path) -> None:
    """The connection must be closed once the request ends — keeping it
    open would leak file handles and confuse SQLite's WAL checkpointer."""
    app, settings = _make_app(tmp_path)
    leaked: list[sqlite3.Connection] = []

    @app.get("/_dep_check_close")
    async def _check(
        conn: Annotated[sqlite3.Connection, Depends(get_db_conn)],
    ) -> dict[str, str]:
        leaked.append(conn)  # keep a reference past request scope
        return {"ok": "yes"}

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        with TestClient(app) as test_client:
            test_client.get("/_dep_check_close")

    assert len(leaked) == 1
    # Operating on a closed connection raises ProgrammingError.
    with pytest.raises(sqlite3.ProgrammingError):
        leaked[0].execute("SELECT 1")


def test_get_db_conn_distinct_connections_across_requests(tmp_path: Path) -> None:
    """Two requests should each get their own connection object."""
    app, settings = _make_app(tmp_path)
    conns: list[sqlite3.Connection] = []

    @app.get("/_dep_check_distinct")
    async def _check(
        conn: Annotated[sqlite3.Connection, Depends(get_db_conn)],
    ) -> dict[str, str]:
        conns.append(conn)
        return {"ok": "yes"}

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        with TestClient(app) as test_client:
            test_client.get("/_dep_check_distinct")
            test_client.get("/_dep_check_distinct")

    assert len(conns) == 2
    assert conns[0] is not conns[1]


def test_get_db_conn_uses_request_scoped_settings(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Even when the global Settings cache points elsewhere, the dep
    must use ``request.app.state.settings`` so test apps with overridden
    ``state_dir`` see their own DB."""
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    monkeypatch.setenv("SCUFRIS_STATE_DIR", str(other_dir))
    monkeypatch.setenv("OPENCODE_URL", "http://wrong.example")

    app, settings = _make_app(tmp_path)  # app has tmp_path, env says other_dir
    seen: list[Path] = []

    @app.get("/_dep_check_settings")
    async def _check(
        conn: Annotated[sqlite3.Connection, Depends(get_db_conn)],
    ) -> dict[str, str]:
        row = conn.execute("PRAGMA database_list").fetchone()
        seen.append(Path(row["file"]))
        return {"ok": "yes"}

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        with TestClient(app) as test_client:
            test_client.get("/_dep_check_settings")

    assert len(seen) == 1
    assert seen[0].parent == tmp_path
    assert seen[0].name == "scufris.sqlite"


def test_get_db_conn_rolls_back_on_handler_exception(tmp_path: Path) -> None:
    """If a handler INSERTs and then crashes, the row must not persist —
    SQLite rolls uncommitted transactions back at close."""
    app, settings = _make_app(tmp_path)

    @app.post("/_dep_check_rollback")
    async def _check(
        conn: Annotated[sqlite3.Connection, Depends(get_db_conn)],
    ) -> None:
        # user_id=1 is seeded by the lifespan.
        conn.execute(
            "INSERT INTO channels (user_id, surface, surface_id, agent) "
            "VALUES (?, ?, ?, ?)",
            (1, "test", "abandon-me", "build"),
        )
        # No commit — and now we crash.
        raise RuntimeError("boom")

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        with TestClient(app, raise_server_exceptions=False) as test_client:
            resp = test_client.post("/_dep_check_rollback")

    assert resp.status_code == 500

    # New connection: confirm the INSERT did not stick.
    from scufris_server.store import connect

    with connect(settings) as conn:
        rows = list(
            conn.execute("SELECT * FROM channels WHERE surface_id = ?", ("abandon-me",))
        )
    assert rows == []


# ---------------------------------------------------------------------------
# Generator semantics — make sure the dep is the async-CM flavour FastAPI
# expects (it must run cleanup after the handler returns).
# ---------------------------------------------------------------------------


def test_get_db_conn_is_async_generator() -> None:
    """FastAPI's Depends() distinguishes plain returns from generators
    (the latter run cleanup after the handler). ``get_db_conn`` must
    be an *async* generator: SQLite connections are thread-pinned, so
    we deliberately keep the dep on the event-loop thread alongside
    the async handler."""
    assert inspect.isasyncgenfunction(get_db_conn)


@pytest_asyncio.fixture
async def _drive_get_db_conn(tmp_path: Path) -> AsyncIterator[sqlite3.Connection]:
    """Direct (non-FastAPI) drive of the dep so we can verify CM semantics
    without spinning up a full app + TestClient."""
    from unittest.mock import Mock

    settings = Settings(state_dir=tmp_path, opencode_url=OPENCODE_TEST_URL)
    fake_request = Mock()
    fake_request.app.state.settings = settings

    agen = get_db_conn(fake_request)
    conn = await agen.__anext__()
    try:
        yield conn
    finally:
        # Drive the generator to completion to trigger close.
        with pytest.raises(StopAsyncIteration):
            await agen.__anext__()


@pytest.mark.asyncio
async def test_get_db_conn_iterator_yields_then_closes(
    _drive_get_db_conn: sqlite3.Connection,
) -> None:
    """Inside the fixture's ``yield`` window, the conn is alive."""
    assert isinstance(_drive_get_db_conn, sqlite3.Connection)
    rows = _drive_get_db_conn.execute("SELECT 1 AS x").fetchall()
    assert rows[0]["x"] == 1


@pytest.mark.asyncio
async def test_get_db_conn_closes_after_iterator_exhausted(tmp_path: Path) -> None:
    """After the async generator is exhausted, the conn is closed."""
    from unittest.mock import Mock

    settings = Settings(state_dir=tmp_path, opencode_url=OPENCODE_TEST_URL)
    fake_request = Mock()
    fake_request.app.state.settings = settings

    agen = get_db_conn(fake_request)
    conn = await agen.__anext__()
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")

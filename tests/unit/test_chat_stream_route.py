"""Tests for ``scufris_server.routes.chat_stream`` (#11 step 10).

Twelve scenarios covering the streaming chat surface end-to-end:

1. Happy path: one thinking event + terminal ``done``.
2. Multiple thinking events surface in order.
3. SSE wire format: ``event:`` / ``data:`` / blank-line separator.
4. Existing session reused (``_touch_session`` invoked, no
   ``create_session`` HTTP call).
5. New session created (``create_channel_link`` row persisted).
6. 503 before stream: default model not cached.
7. 503 before stream: ``create_session`` network error.
8. 503 before stream: ``create_session`` 5xx.
9. Mid-stream bus reconnect → ``error`` event with
   ``error_type: "BusReconnected"``.
10. Mid-stream ``send_message`` 5xx → ``error`` event with
    ``error_type: "OpencodeServerError"``.
11. Identity override (#12 D4) pins ``channels.user_id`` regardless
    of TOML.
12. Subagent tool-call event (``message.part.updated`` with
    ``part.tool="knowledge_agent"``) surfaces as a ``thinking``
    SSE event with ``kind="tool_call"``.

Plus a 13th sanity check that ``POST /v1/chat/stream`` is mounted.

The :class:`EventBus` dependency is overridden with a
:class:`FakeEventBus` test double — it accepts pre-loaded events
that are pushed onto the per-session queue at subscribe time, so
the drain loop sees them before the background ``send_message``
task resolves. Tests that exercise the bus-subscribe failure path
construct a separate :class:`FakeEventBus(raise_on_subscribe=...)`
instance.

Opencode is mocked via :mod:`respx`; the SQLite store is real
(one file per test, ``tmp_path``). Mirrors ``test_chat_route.py``
conventions so the two suites read as siblings.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from scufris_server.app import create_app
from scufris_server.config import Settings, get_settings
from scufris_server.dependencies import get_event_bus
from scufris_server.opencode_client import _BusReconnected
from scufris_server.store import connect

OPENCODE_TEST_URL = "http://opencode.test"
HEALTH_OK_BODY = {"healthy": True, "version": "1.15.13"}
PROVIDER_OK_BODY: dict[str, Any] = {
    "all": [],
    "default": {"ollama": "qwen3:latest"},
    "connected": ["ollama"],
}
PROVIDER_EMPTY_BODY: dict[str, Any] = {
    "all": [],
    "default": {},
    "connected": [],
}


# ---------------------------------------------------------------------------
# FakeEventBus — duck-typed stand-in for EventBus
# ---------------------------------------------------------------------------


class FakeEventBus:
    """In-memory test double for :class:`scufris_server.events.EventBus`.

    Doesn't connect to anything. Tests pre-load events via
    :meth:`preload`; on subscribe those events are immediately put
    onto the per-session queue so the drain loop in
    :mod:`scufris_server.routes.chat_stream` sees them before the
    background ``send_message`` task's terminal sentinel arrives.

    Construct with ``raise_on_subscribe=OpencodeUnavailable(...)``
    to exercise the bus-cold-start failure path.

    Not a subclass of :class:`EventBus` — duck-typed because the
    real bus's ``__init__`` requires a live ``httpx.AsyncClient``
    that we'd have nothing to wire it to. FastAPI's
    ``dependency_overrides`` doesn't enforce types at runtime, so
    this works.
    """

    def __init__(self, *, raise_on_subscribe: Exception | None = None) -> None:
        self._preloaded: dict[str, list[Any]] = {}
        self._queues: dict[str, list[asyncio.Queue[Any]]] = {}
        self._raise_on_subscribe = raise_on_subscribe
        self.subscribe_calls: list[str] = []

    def preload(self, session_id: str, events: list[Any]) -> None:
        """Queue events to be pushed at the next :meth:`subscribe`."""
        self._preloaded.setdefault(session_id, []).extend(events)

    @contextlib.asynccontextmanager
    async def subscribe(
        self,
        session_id: str,
        *,
        connect_timeout: float | None = None,  # noqa: ARG002 — API parity
    ) -> AsyncIterator[asyncio.Queue[Any]]:
        """Register a queue for the duration of the block (API-compatible
        with :meth:`scufris_server.events.EventBus.subscribe`)."""
        self.subscribe_calls.append(session_id)
        if self._raise_on_subscribe is not None:
            raise self._raise_on_subscribe
        q: asyncio.Queue[Any] = asyncio.Queue()
        self._queues.setdefault(session_id, []).append(q)
        for ev in self._preloaded.get(session_id, []):
            await q.put(ev)
        try:
            yield q
        finally:
            self._queues[session_id].remove(q)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assistant_body(
    *,
    msg_id: str = "msg_demo",
    session_id: str = "ses_demo",
    text: str = "hi there",
    tokens_in: int = 7,
    tokens_out: int = 11,
    cost: float = 0.00042,
) -> dict[str, Any]:
    """Minimal ``{info, parts}`` body opencode returns from /message."""
    return {
        "info": {
            "id": msg_id,
            "sessionID": session_id,
            "role": "assistant",
            "providerID": "ollama",
            "modelID": "qwen3:latest",
            "tokens": {
                "input": tokens_in,
                "output": tokens_out,
                "reasoning": 0,
                "total": tokens_in + tokens_out,
                "cache": {"read": 0, "write": 0},
            },
            "cost": cost,
        },
        "parts": [
            {"type": "step-start"},
            {"type": "text", "text": text},
            {"type": "step-finish"},
        ],
    }


def _session_body(session_id: str = "ses_new") -> dict[str, Any]:
    return {"id": session_id, "slug": "scufris", "projectID": "prj_x"}


def _channel_payload(
    surface: str = "cli", surface_id: str = "term-1", agent: str = "build"
) -> dict[str, Any]:
    return {
        "message": "hello",
        "channel": {
            "surface": surface,
            "surface_id": surface_id,
            "agent": agent,
        },
    }


def _delta_event(delta: str, *, session_id: str = "ses_x") -> dict[str, Any]:
    """Construct a ``message.part.delta`` event with the given text delta.

    The bus dispatch normally filters by ``properties.sessionID``;
    :class:`FakeEventBus` skips that step (the test puts events
    directly onto the per-session queue), but we keep ``sessionID``
    in the dict for realism so the wire-format-assertion test is
    representative of production events.
    """
    return {
        "type": "message.part.delta",
        "properties": {
            "sessionID": session_id,
            "field": "text",
            "delta": delta,
        },
    }


def _tool_running_event(
    part_id: str,
    tool: str = "bash",
    *,
    session_id: str = "ses_x",
    title: str | None = None,
) -> dict[str, Any]:
    """Construct a ``message.part.updated`` event for a tool entering
    ``running`` — this is what produces ``tool_call`` thinking events."""
    state: dict[str, Any] = {"status": "running"}
    if title is not None:
        state["title"] = title
    return {
        "type": "message.part.updated",
        "properties": {
            "sessionID": session_id,
            "part": {
                "id": part_id,
                "type": "tool",
                "tool": tool,
                "state": state,
            },
        },
    }


def _parse_sse(body: bytes) -> list[tuple[str, dict[str, Any]]]:
    """Decode an SSE byte-stream into ``[(event_type, data_dict), ...]``.

    Ignores comment-only records (``:keepalive``). The handler under
    test always pairs ``event:`` with ``data:`` so we require both
    fields per record.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    for raw_record in body.decode("utf-8").split("\n\n"):
        record = raw_record.strip()
        if not record:
            continue
        if all(line.startswith(":") for line in record.split("\n")):
            continue  # comment-only (keepalive)
        event_type: str | None = None
        data_text: str | None = None
        for line in record.split("\n"):
            if line.startswith("event:"):
                event_type = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_text = line[len("data:") :].strip()
        if event_type is not None and data_text is not None:
            events.append((event_type, json.loads(data_text)))
    return events


def _read_stream_body(client: TestClient, payload: dict[str, Any]) -> bytes:
    """POST /v1/chat/stream and return the full SSE response body bytes."""
    with client.stream("POST", "/v1/chat/stream", json=payload) as resp:
        assert resp.status_code == 200, (
            f"unexpected status {resp.status_code}: {resp.read()!r}"
        )
        return b"".join(resp.iter_bytes())


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(state_dir=tmp_path, opencode_url=OPENCODE_TEST_URL)


def _mock_happy_boot(mock: respx.MockRouter) -> None:
    """Stub the lifespan-required endpoints (health + provider)."""
    mock.get("/global/health").mock(return_value=Response(200, json=HEALTH_OK_BODY))
    mock.get("/provider").mock(return_value=Response(200, json=PROVIDER_OK_BODY))


def _override_bus(app: Any, bus: FakeEventBus) -> None:
    """Wire ``bus`` as the ``get_event_bus`` dependency for the test app."""
    app.dependency_overrides[get_event_bus] = lambda: bus


def _write_config_toml(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. Happy path — one thinking event + done
# ---------------------------------------------------------------------------


def test_chat_stream_happy_path_emits_thinking_then_done(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    fake_bus = FakeEventBus()
    fake_bus.preload("ses_happy", [_delta_event("hi", session_id="ses_happy")])
    _override_bus(app, fake_bus)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(
            return_value=Response(200, json=_session_body("ses_happy"))
        )
        mock.post("/session/ses_happy/message").mock(
            return_value=Response(
                200,
                json=_assistant_body(
                    msg_id="msg_h", session_id="ses_happy", text="hi there"
                ),
            )
        )

        with TestClient(app) as client:
            body = _read_stream_body(client, _channel_payload())

    events = _parse_sse(body)
    # First event is the thinking text delta; last event is done.
    assert events[0] == (
        "thinking",
        {"kind": "text", "source": "scufris", "text": "hi", "depth": 0},
    )
    assert events[-1][0] == "done"
    done_body = events[-1][1]
    assert done_body["type"] == "done"
    assert done_body["message"] == "hi there"
    assert done_body["oc_session_id"] == "ses_happy"
    assert done_body["oc_message_id"] == "msg_h"
    assert done_body["tokens"] == {"input": 7, "output": 11}
    assert done_body["cost"] == pytest.approx(0.00042)
    # Bus subscribe was called exactly once with the correct id.
    assert fake_bus.subscribe_calls == ["ses_happy"]


# ---------------------------------------------------------------------------
# 2. Multiple thinking events in order
# ---------------------------------------------------------------------------


def test_chat_stream_emits_multiple_thinking_events_in_order(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    fake_bus = FakeEventBus()
    fake_bus.preload(
        "ses_multi",
        [
            _delta_event("foo", session_id="ses_multi"),
            _delta_event("bar", session_id="ses_multi"),
            _delta_event("baz", session_id="ses_multi"),
        ],
    )
    _override_bus(app, fake_bus)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(
            return_value=Response(200, json=_session_body("ses_multi"))
        )
        mock.post("/session/ses_multi/message").mock(
            return_value=Response(
                200,
                json=_assistant_body(
                    msg_id="m", session_id="ses_multi", text="foobarbaz"
                ),
            )
        )

        with TestClient(app) as client:
            body = _read_stream_body(client, _channel_payload())

    events = _parse_sse(body)
    thinking = [(t, d) for t, d in events if t == "thinking"]
    assert [d["text"] for _, d in thinking] == ["foo", "bar", "baz"]
    # Terminal done present.
    assert events[-1][0] == "done"


# ---------------------------------------------------------------------------
# 3. SSE wire format: event/data/blank-line framing
# ---------------------------------------------------------------------------


def test_chat_stream_wire_format_is_event_data_blank_line(tmp_path: Path) -> None:
    """Each record must be ``event: <type>\\ndata: <json>\\n\\n``."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    fake_bus = FakeEventBus()
    fake_bus.preload("ses_wire", [_delta_event("x", session_id="ses_wire")])
    _override_bus(app, fake_bus)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(
            return_value=Response(200, json=_session_body("ses_wire"))
        )
        mock.post("/session/ses_wire/message").mock(
            return_value=Response(
                200, json=_assistant_body(msg_id="m", session_id="ses_wire", text="x")
            )
        )

        with TestClient(app) as client:
            with client.stream(
                "POST", "/v1/chat/stream", json=_channel_payload()
            ) as resp:
                assert resp.headers["content-type"].startswith("text/event-stream")
                assert resp.headers["cache-control"] == "no-cache"
                assert resp.headers["x-accel-buffering"] == "no"
                raw = b"".join(resp.iter_bytes())

    text = raw.decode("utf-8")
    # At least one ``event: thinking\ndata: {...}\n\n`` block.
    assert "event: thinking\ndata: " in text
    # Terminal done block.
    assert "event: done\ndata: " in text
    # JSON is compact — no spaces around separators.
    assert '"kind":"text"' in text
    assert '"kind": "text"' not in text
    # Records are blank-line-separated.
    assert text.count("\n\n") >= 2  # at least one thinking + one done


# ---------------------------------------------------------------------------
# 4. Existing session reused
# ---------------------------------------------------------------------------


def test_chat_stream_reuses_existing_session(tmp_path: Path) -> None:
    """Pre-existing ``session_links`` row means no ``create_session``
    HTTP call; ``_touch_session`` bumps ``last_used_at``."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    fake_bus = FakeEventBus()
    _override_bus(app, fake_bus)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        create_route = mock.post("/session").mock(
            return_value=Response(200, json=_session_body("ses_reuse"))
        )
        mock.post("/session/ses_reuse/message").mock(
            return_value=Response(
                200, json=_assistant_body(msg_id="m1", session_id="ses_reuse")
            )
        )

        with TestClient(app) as client:
            # First call — creates the session + link.
            body1 = _read_stream_body(client, _channel_payload())

            # Backdate ``last_used_at`` so the bump on reuse is detectable.
            with connect(settings) as direct_conn:
                with direct_conn:
                    direct_conn.execute(
                        "UPDATE session_links SET last_used_at = 1, "
                        "created_at = 1 WHERE oc_session_id = 'ses_reuse'"
                    )

            # Second call — must reuse.
            body2 = _read_stream_body(client, _channel_payload())

    # Both replies routed to the same opencode session.
    done1 = _parse_sse(body1)[-1][1]
    done2 = _parse_sse(body2)[-1][1]
    assert done1["oc_session_id"] == done2["oc_session_id"] == "ses_reuse"

    # Only one create_session call total.
    assert create_route.call_count == 1

    # last_used_at was bumped on the second turn.
    with connect(settings) as conn:
        row = conn.execute(
            "SELECT created_at, last_used_at FROM session_links "
            "WHERE oc_session_id = 'ses_reuse'"
        ).fetchone()
    assert row is not None
    assert row["created_at"] == 1
    assert row["last_used_at"] > 1


# ---------------------------------------------------------------------------
# 5. New session created — channel + session_link rows persisted
# ---------------------------------------------------------------------------


def test_chat_stream_creates_session_when_no_link(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    fake_bus = FakeEventBus()
    _override_bus(app, fake_bus)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        create_route = mock.post("/session").mock(
            return_value=Response(200, json=_session_body("ses_new"))
        )
        mock.post("/session/ses_new/message").mock(
            return_value=Response(
                200, json=_assistant_body(msg_id="m", session_id="ses_new")
            )
        )

        with TestClient(app) as client:
            _read_stream_body(client, _channel_payload())

    assert create_route.call_count == 1

    # Channel + link rows materialised.
    with connect(settings) as conn:
        channel_row = conn.execute(
            "SELECT user_id, surface, surface_id, agent FROM channels"
        ).fetchone()
        link_row = conn.execute(
            "SELECT oc_session_id, channel_id, created_at, last_used_at "
            "FROM session_links"
        ).fetchone()

    assert channel_row is not None
    assert (
        channel_row["user_id"],
        channel_row["surface"],
        channel_row["surface_id"],
        channel_row["agent"],
    ) == (1, "cli", "term-1", "build")
    assert link_row is not None
    assert link_row["oc_session_id"] == "ses_new"
    assert link_row["created_at"] == link_row["last_used_at"]


# ---------------------------------------------------------------------------
# 6. 503 before stream — default model missing
# ---------------------------------------------------------------------------


def test_chat_stream_returns_503_when_no_default_model_cached(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    # No bus override needed — handler never reaches the dep when
    # the default-model gate fails, but FastAPI still resolves it
    # before calling the handler. Provide an empty fake to keep
    # the dep happy.
    _override_bus(app, FakeEventBus())

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(return_value=Response(200, json=HEALTH_OK_BODY))
        mock.get("/provider").mock(return_value=Response(200, json=PROVIDER_EMPTY_BODY))
        # No /session* mocks — handler must NOT reach opencode.

        with TestClient(app) as client:
            assert app.state.opencode_default_model is None
            resp = client.post("/v1/chat/stream", json=_channel_payload())

    assert resp.status_code == 503
    body = resp.json()
    assert body["detail"]["error_type"] == "DefaultModelMissing"
    assert "default model" in body["detail"]["error"].lower()


# ---------------------------------------------------------------------------
# 7. 503 before stream — create_session network error
# ---------------------------------------------------------------------------


def test_chat_stream_returns_503_when_create_session_network_error(
    tmp_path: Path,
) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    _override_bus(app, FakeEventBus())

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(side_effect=httpx.ConnectError("refused"))

        with TestClient(app) as client:
            resp = client.post("/v1/chat/stream", json=_channel_payload())

    assert resp.status_code == 503
    assert resp.json()["detail"]["error_type"] == "OpencodeNetworkError"


# ---------------------------------------------------------------------------
# 8. 503 before stream — create_session 5xx
# ---------------------------------------------------------------------------


def test_chat_stream_returns_503_when_create_session_5xx(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    _override_bus(app, FakeEventBus())

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(return_value=Response(503, text="overloaded"))

        with TestClient(app) as client:
            resp = client.post("/v1/chat/stream", json=_channel_payload())

    assert resp.status_code == 503
    assert resp.json()["detail"]["error_type"] == "OpencodeServerError"


# ---------------------------------------------------------------------------
# 9. Mid-stream bus reconnect → error event
# ---------------------------------------------------------------------------


def test_chat_stream_emits_error_on_bus_reconnect(tmp_path: Path) -> None:
    """A :class:`_BusReconnected` sentinel on the queue terminates
    the stream with an ``error`` event (#11 D4 — fail-the-turn policy)."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    fake_bus = FakeEventBus()
    fake_bus.preload("ses_recon", [_BusReconnected()])
    _override_bus(app, fake_bus)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(
            return_value=Response(200, json=_session_body("ses_recon"))
        )
        mock.post("/session/ses_recon/message").mock(
            return_value=Response(
                200, json=_assistant_body(msg_id="m", session_id="ses_recon")
            )
        )

        with TestClient(app) as client:
            body = _read_stream_body(client, _channel_payload())

    events = _parse_sse(body)
    # Exactly one event — the error — and no done.
    assert len(events) == 1
    assert events[0][0] == "error"
    assert events[0][1]["error_type"] == "BusReconnected"
    assert "reconnect" in events[0][1]["error"].lower()


# ---------------------------------------------------------------------------
# 10. Mid-stream send_message 5xx → error event
# ---------------------------------------------------------------------------


def test_chat_stream_emits_error_on_send_message_5xx(tmp_path: Path) -> None:
    """``send_message`` 5xx after the stream has begun must be
    tunnelled through ``event: error`` (we can't switch to HTTP 5xx
    once StreamingResponse has committed headers)."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    fake_bus = FakeEventBus()
    _override_bus(app, fake_bus)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(
            return_value=Response(200, json=_session_body("ses_err"))
        )
        mock.post("/session/ses_err/message").mock(
            return_value=Response(503, text="overloaded")
        )

        with TestClient(app) as client:
            body = _read_stream_body(client, _channel_payload())

    events = _parse_sse(body)
    assert len(events) == 1
    assert events[0][0] == "error"
    assert events[0][1]["error_type"] == "OpencodeServerError"
    assert events[0][1]["error"]


# ---------------------------------------------------------------------------
# 11. Identity override respected
# ---------------------------------------------------------------------------


def test_chat_stream_identity_override_pins_user_id(tmp_path: Path) -> None:
    """``Settings(user_id=1)`` short-circuits the resolver: even with
    a TOML mapping that would route ``(cli, alex)`` to a non-default
    user, the persisted ``channels.user_id`` must be 1 and no
    ``surface_bindings`` row should be written (#12 D4 — override
    skips materialisation)."""
    config = _write_config_toml(
        tmp_path / "config.toml",
        '[user]\nusername = "alex"\n[user.identity]\ncli = "alex"\n',
    )
    settings = Settings(
        state_dir=tmp_path,
        opencode_url=OPENCODE_TEST_URL,
        config_path=config,
        user_id=1,
    )
    app = create_app(settings)
    fake_bus = FakeEventBus()
    _override_bus(app, fake_bus)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(
            return_value=Response(200, json=_session_body("ses_o"))
        )
        mock.post("/session/ses_o/message").mock(
            return_value=Response(
                200, json=_assistant_body(msg_id="m_o", session_id="ses_o")
            )
        )

        with TestClient(app) as client:
            _read_stream_body(
                client,
                _channel_payload(surface="cli", surface_id="alex"),
            )

    with connect(settings) as conn:
        channel_row = conn.execute("SELECT user_id FROM channels").fetchone()
        n_bindings = conn.execute(
            "SELECT COUNT(*) AS n FROM surface_bindings"
        ).fetchone()["n"]
        usernames = {r["username"] for r in conn.execute("SELECT username FROM users")}

    assert channel_row is not None
    assert channel_row["user_id"] == 1
    assert n_bindings == 0
    # The TOML user was never auto-created — override short-circuits
    # before the materialise step.
    assert usernames == {"default"}


# ---------------------------------------------------------------------------
# 12. Subagent tool_call event surfaces as thinking
# ---------------------------------------------------------------------------


def test_chat_stream_subagent_tool_call_surfaces_as_thinking(tmp_path: Path) -> None:
    """A ``message.part.updated`` event for ``part.tool="knowledge_agent"``
    in ``state.status="running"`` must map to a ``thinking`` SSE event
    with ``kind="tool_call"`` and ``text="knowledge_agent"`` — the
    rendering surface the v1 CLI uses to detect sub-agent spawns."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    fake_bus = FakeEventBus()
    fake_bus.preload(
        "ses_sub",
        [
            _tool_running_event(
                "part_sub_1",
                tool="knowledge_agent",
                session_id="ses_sub",
                title="recall: pricing FAQ",
            )
        ],
    )
    _override_bus(app, fake_bus)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(
            return_value=Response(200, json=_session_body("ses_sub"))
        )
        mock.post("/session/ses_sub/message").mock(
            return_value=Response(
                200, json=_assistant_body(msg_id="m", session_id="ses_sub")
            )
        )

        with TestClient(app) as client:
            body = _read_stream_body(client, _channel_payload())

    events = _parse_sse(body)
    thinking = [d for t, d in events if t == "thinking"]
    assert len(thinking) == 1
    assert thinking[0]["kind"] == "tool_call"
    assert thinking[0]["text"] == "knowledge_agent"
    assert thinking[0]["arg"] == "recall: pricing FAQ"
    # Terminal done.
    assert events[-1][0] == "done"


# ---------------------------------------------------------------------------
# Mounting smoke check
# ---------------------------------------------------------------------------


def test_chat_stream_route_is_mounted(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    paths = {r.path for r in app.routes}  # type: ignore[attr-defined]
    assert "/v1/chat/stream" in paths

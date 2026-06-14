"""Tests for ``scufris_server.routes.chat`` (step 8 of #9).

Three scenarios from TASK.md plus a few edges we discovered while
implementing:

1. First call to a channel: creates an opencode session, persists
   ``channels`` + ``session_links`` rows.
2. Second call to the *same* channel: reuses the cached session
   (no new ``create_session`` call to opencode, ``last_used_at``
   bumped).
3. Opencode 503 on ``send_message`` → /v1/chat 503 with
   ``{error, error_type}``.
4. Opencode network error on ``create_session`` → /v1/chat 503.
5. ``app.state.opencode_default_model`` is ``None`` (degraded boot or
   no connected provider) → /v1/chat 503 with
   ``error_type=DefaultModelMissing``.

Opencode is mocked via :mod:`respx`; the SQLite store is real (one
file per test, ``tmp_path``). We never spin up a real opencode
process here — that's covered by ``tests/integration`` (step 9).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from scufris_server.app import create_app
from scufris_server.config import Settings, get_settings
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


def _assistant_body(
    *,
    msg_id: str = "msg_demo",
    session_id: str = "ses_demo",
    text: str = "hi there",
    tokens_in: int = 7,
    tokens_out: int = 11,
    cost: float = 0.00042,
) -> dict[str, Any]:
    """Construct a minimal {info, parts} response opencode would send back."""
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


def _write_config_toml(path: Path, body: str) -> Path:
    """Write ``body`` to ``path`` with parents created. Returns ``path``.

    Used by the #12 identity scenarios below to drive
    ``Settings.config_path`` from a per-test fixture file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Happy path — first call creates, second call reuses
# ---------------------------------------------------------------------------


def test_chat_first_call_creates_session_and_persists_rows(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        create_route = mock.post("/session").mock(
            return_value=Response(200, json=_session_body("ses_first"))
        )
        send_route = mock.post("/session/ses_first/message").mock(
            return_value=Response(
                200, json=_assistant_body(msg_id="msg_a", session_id="ses_first")
            )
        )

        with TestClient(app) as client:
            resp = client.post("/v1/chat", json=_channel_payload())

    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == "hi there"
    assert body["oc_session_id"] == "ses_first"
    assert body["oc_message_id"] == "msg_a"
    assert body["tokens"] == {"input": 7, "output": 11}
    assert body["cost"] == pytest.approx(0.00042)

    # opencode received exactly one create + one send.
    assert create_route.call_count == 1
    assert send_route.call_count == 1

    # DB rows persisted.
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
    assert link_row["oc_session_id"] == "ses_first"
    assert link_row["created_at"] == link_row["last_used_at"]


def test_chat_second_call_reuses_existing_session(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        create_route = mock.post("/session").mock(
            return_value=Response(200, json=_session_body("ses_first"))
        )
        send_route = mock.post("/session/ses_first/message").mock(
            return_value=Response(
                200, json=_assistant_body(msg_id="msg_2x", session_id="ses_first")
            )
        )

        with TestClient(app) as client:
            r1 = client.post("/v1/chat", json=_channel_payload())
            r2 = client.post("/v1/chat", json=_channel_payload())

    assert r1.status_code == 200
    assert r2.status_code == 200
    # Both replies route to the same opencode session.
    assert r1.json()["oc_session_id"] == r2.json()["oc_session_id"] == "ses_first"
    # Only one create_session call total — second turn reused.
    assert create_route.call_count == 1
    # Two send_message calls.
    assert send_route.call_count == 2

    # Exactly one channel row, one link row.
    with connect(settings) as conn:
        n_channels = conn.execute("SELECT COUNT(*) AS n FROM channels").fetchone()["n"]
        n_links = conn.execute("SELECT COUNT(*) AS n FROM session_links").fetchone()[
            "n"
        ]
    assert n_channels == 1
    assert n_links == 1


def test_chat_different_channel_creates_a_new_session(tmp_path: Path) -> None:
    """``(surface, surface_id, agent)`` is the channel key; a different
    triple gets its own opencode session even for the same user."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    session_ids = iter(["ses_cli", "ses_telegram"])

    def _create_session_handler(_request: Any) -> Response:
        return Response(200, json=_session_body(next(session_ids)))

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(side_effect=_create_session_handler)
        mock.post("/session/ses_cli/message").mock(
            return_value=Response(
                200, json=_assistant_body(msg_id="m1", session_id="ses_cli")
            )
        )
        mock.post("/session/ses_telegram/message").mock(
            return_value=Response(
                200, json=_assistant_body(msg_id="m2", session_id="ses_telegram")
            )
        )

        with TestClient(app) as client:
            r1 = client.post(
                "/v1/chat", json=_channel_payload(surface="cli", surface_id="term-1")
            )
            r2 = client.post(
                "/v1/chat",
                json=_channel_payload(surface="telegram", surface_id="chat-99"),
            )

    assert r1.json()["oc_session_id"] == "ses_cli"
    assert r2.json()["oc_session_id"] == "ses_telegram"

    with connect(settings) as conn:
        n_channels = conn.execute("SELECT COUNT(*) AS n FROM channels").fetchone()["n"]
    assert n_channels == 2


# ---------------------------------------------------------------------------
# Error mapping: opencode 5xx / network → /v1/chat 503
# ---------------------------------------------------------------------------


def test_chat_returns_503_when_send_message_5xx(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(
            return_value=Response(200, json=_session_body("ses_x"))
        )
        mock.post("/session/ses_x/message").mock(
            return_value=Response(503, text="overloaded")
        )

        with TestClient(app) as client:
            resp = client.post("/v1/chat", json=_channel_payload())

    assert resp.status_code == 503
    body = resp.json()
    # FastAPI wraps our HTTPException(detail=dict) into {"detail": dict}.
    assert body["detail"]["error_type"] == "OpencodeServerError"
    assert body["detail"]["error"]


def test_chat_returns_503_when_create_session_network_error(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(side_effect=httpx.ConnectError("refused"))

        with TestClient(app) as client:
            resp = client.post("/v1/chat", json=_channel_payload())

    assert resp.status_code == 503
    body = resp.json()
    assert body["detail"]["error_type"] == "OpencodeNetworkError"


def test_chat_returns_503_when_create_session_5xx(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(return_value=Response(503, text="overloaded"))

        with TestClient(app) as client:
            resp = client.post("/v1/chat", json=_channel_payload())

    assert resp.status_code == 503
    assert resp.json()["detail"]["error_type"] == "OpencodeServerError"


def test_chat_does_not_persist_rows_when_create_session_fails(tmp_path: Path) -> None:
    """A failed create_session must not leave half-written channel/link rows."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(side_effect=httpx.ConnectError("refused"))

        with TestClient(app) as client:
            resp = client.post("/v1/chat", json=_channel_payload())

    assert resp.status_code == 503

    with connect(settings) as conn:
        n_channels = conn.execute("SELECT COUNT(*) AS n FROM channels").fetchone()["n"]
        n_links = conn.execute("SELECT COUNT(*) AS n FROM session_links").fetchone()[
            "n"
        ]
    assert n_channels == 0
    assert n_links == 0


# ---------------------------------------------------------------------------
# Default-model gate
# ---------------------------------------------------------------------------


def test_chat_returns_503_when_no_default_model_cached(tmp_path: Path) -> None:
    """Lifespan booted with health up but /provider returned an empty
    connected list → ``app.state.opencode_default_model`` is None →
    /v1/chat refuses."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(return_value=Response(200, json=HEALTH_OK_BODY))
        mock.get("/provider").mock(return_value=Response(200, json=PROVIDER_EMPTY_BODY))
        # Crucially, no /session* mocks: the handler must NEVER reach
        # opencode if the default model is missing.
        with TestClient(app) as client:
            assert app.state.opencode_default_model is None
            resp = client.post("/v1/chat", json=_channel_payload())

    assert resp.status_code == 503
    body = resp.json()
    assert body["detail"]["error_type"] == "DefaultModelMissing"
    assert "default model" in body["detail"]["error"].lower()


def test_chat_returns_503_on_degraded_boot(tmp_path: Path) -> None:
    """Health probe failed → no /provider call attempted →
    default_model is None → /v1/chat refuses."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(side_effect=httpx.ConnectError("refused"))

        with TestClient(app) as client:
            resp = client.post("/v1/chat", json=_channel_payload())

    assert resp.status_code == 503
    assert resp.json()["detail"]["error_type"] == "DefaultModelMissing"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_chat_rejects_empty_message(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            payload = _channel_payload()
            payload["message"] = ""
            resp = client.post("/v1/chat", json=payload)

    assert resp.status_code == 422  # pydantic validation


def test_chat_rejects_missing_channel_field(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            resp = client.post("/v1/chat", json={"message": "hi"})

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# send_message wire payload
# ---------------------------------------------------------------------------


def test_chat_sends_default_model_and_agent_to_opencode(tmp_path: Path) -> None:
    """The request body to opencode carries the cached ``ModelRef``
    and the channel's ``agent``. This is the critical contract: if it
    breaks, opencode silently picks an unintended model."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(
            return_value=Response(200, json=_session_body("ses_y"))
        )
        send_route = mock.post("/session/ses_y/message").mock(
            return_value=Response(
                200, json=_assistant_body(msg_id="m", session_id="ses_y")
            )
        )

        with TestClient(app) as client:
            resp = client.post("/v1/chat", json=_channel_payload(agent="explore"))

    assert resp.status_code == 200
    sent = send_route.calls.last.request
    payload = httpx.Request("POST", sent.url, content=sent.content).read()
    import json

    body = json.loads(payload)
    assert body["model"] == {"providerID": "ollama", "modelID": "qwen3:latest"}
    assert body["agent"] == "explore"
    assert body["parts"] == [{"type": "text", "text": "hello"}]


# ---------------------------------------------------------------------------
# touch last_used_at on reuse
# ---------------------------------------------------------------------------


def test_chat_reuse_bumps_last_used_at(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(
            return_value=Response(200, json=_session_body("ses_t"))
        )
        mock.post("/session/ses_t/message").mock(
            return_value=Response(
                200, json=_assistant_body(msg_id="m", session_id="ses_t")
            )
        )

        with TestClient(app) as client:
            client.post("/v1/chat", json=_channel_payload())

            # Flip the row's last_used_at backwards so we can detect
            # the bump on reuse.
            with connect(settings) as direct_conn:
                with direct_conn:
                    direct_conn.execute(
                        "UPDATE session_links SET last_used_at = 1, "
                        "created_at = 1 WHERE oc_session_id = 'ses_t'"
                    )

            client.post("/v1/chat", json=_channel_payload())

    with connect(settings) as conn:
        row = conn.execute(
            "SELECT created_at, last_used_at FROM session_links "
            "WHERE oc_session_id = 'ses_t'"
        ).fetchone()
    assert row is not None
    assert row["created_at"] == 1, "created_at must not change on reuse"
    assert row["last_used_at"] > 1, "last_used_at must be bumped on reuse"


# ---------------------------------------------------------------------------
# Identity (#12) — TOML / override / default-fallback wiring through chat
# ---------------------------------------------------------------------------


def test_chat_binds_to_toml_user_when_surface_id_matches(tmp_path: Path) -> None:
    """A request from ``(cli, alex)`` matching the TOML identity
    mapping must persist a ``channels`` row keyed by the TOML user's
    id (not 1) and materialise a binding row pointing at that id.
    Verifies the ``resolve_user`` call wired in step 9 of #12 flows
    through to the persistence layer."""
    config = _write_config_toml(
        tmp_path / "config.toml",
        '[user]\n'
        'username = "alex"\n'
        '[user.identity]\n'
        'cli = "alex"\n',
    )
    settings = Settings(
        state_dir=tmp_path,
        opencode_url=OPENCODE_TEST_URL,
        config_path=config,
    )
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(
            return_value=Response(200, json=_session_body("ses_alex"))
        )
        mock.post("/session/ses_alex/message").mock(
            return_value=Response(
                200, json=_assistant_body(msg_id="m_alex", session_id="ses_alex")
            )
        )
        with TestClient(app) as client:
            resp = client.post(
                "/v1/chat",
                json=_channel_payload(surface="cli", surface_id="alex"),
            )

    assert resp.status_code == 200

    with connect(settings) as conn:
        users = {
            r["username"]: r["id"]
            for r in conn.execute("SELECT id, username FROM users")
        }
        channel_row = conn.execute(
            "SELECT user_id, surface, surface_id FROM channels"
        ).fetchone()
        bindings = list(
            conn.execute(
                "SELECT user_id, surface, surface_id FROM surface_bindings"
            )
        )

    # Both default and the TOML user exist; alex is not user 1.
    assert "alex" in users
    alex_id = users["alex"]
    assert alex_id != 1

    # channels row keyed by alex.
    assert channel_row is not None
    assert channel_row["user_id"] == alex_id

    # Binding materialised exactly once for the TOML hit.
    assert len(bindings) == 1
    assert (
        bindings[0]["user_id"],
        bindings[0]["surface"],
        bindings[0]["surface_id"],
    ) == (alex_id, "cli", "alex")


def test_chat_with_identity_override_pins_user_id_and_skips_binding(
    tmp_path: Path,
) -> None:
    """``Settings(user_id=1)`` is the server-side override (D4). Even
    when the request matches a TOML identity that *would* route to a
    different user, chat must persist ``channels.user_id=1`` and
    write **no** ``surface_bindings`` row — the override is a
    deliberate detour around the binding table."""
    config = _write_config_toml(
        tmp_path / "config.toml",
        '[user]\n'
        'username = "alex"\n'
        '[user.identity]\n'
        'cli = "alex"\n',
    )
    settings = Settings(
        state_dir=tmp_path,
        opencode_url=OPENCODE_TEST_URL,
        config_path=config,
        user_id=1,
    )
    app = create_app(settings)

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
            # Request mirrors the TOML identity entry; override must
            # still dominate.
            resp = client.post(
                "/v1/chat",
                json=_channel_payload(surface="cli", surface_id="alex"),
            )

    assert resp.status_code == 200

    with connect(settings) as conn:
        channel_row = conn.execute("SELECT user_id FROM channels").fetchone()
        n_bindings = conn.execute(
            "SELECT COUNT(*) AS n FROM surface_bindings"
        ).fetchone()["n"]
        usernames = {
            r["username"] for r in conn.execute("SELECT username FROM users")
        }

    assert channel_row is not None
    assert channel_row["user_id"] == 1
    # Override path skips materialisation.
    assert n_bindings == 0
    # The TOML user was never auto-created either — override
    # short-circuits before _ensure_user_row runs.
    assert usernames == {"default"}


def test_chat_default_fallback_writes_binding_then_reuses_it(
    tmp_path: Path,
) -> None:
    """Two chats from the same unknown ``(surface, surface_id)`` pair
    must hit the binding cache the second time. After both calls
    there should be exactly one ``surface_bindings`` row keyed at
    ``user_id=1`` and one ``channels`` row reused across both
    turns."""
    settings = _make_settings(tmp_path)  # no TOML, no override
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        create_route = mock.post("/session").mock(
            return_value=Response(200, json=_session_body("ses_d"))
        )
        mock.post("/session/ses_d/message").mock(
            return_value=Response(
                200, json=_assistant_body(msg_id="m_d", session_id="ses_d")
            )
        )
        with TestClient(app) as client:
            r1 = client.post(
                "/v1/chat",
                json=_channel_payload(surface="cli", surface_id="term-x"),
            )
            r2 = client.post(
                "/v1/chat",
                json=_channel_payload(surface="cli", surface_id="term-x"),
            )

    assert r1.status_code == 200
    assert r2.status_code == 200

    # Channel reused → only one create_session call.
    assert create_route.call_count == 1

    with connect(settings) as conn:
        bindings = list(
            conn.execute(
                "SELECT user_id, surface, surface_id FROM surface_bindings"
            )
        )
        n_channels = conn.execute(
            "SELECT COUNT(*) AS n FROM channels"
        ).fetchone()["n"]

    # Binding materialised once on first call; cache hit on second.
    assert len(bindings) == 1
    assert (
        bindings[0]["user_id"],
        bindings[0]["surface"],
        bindings[0]["surface_id"],
    ) == (1, "cli", "term-x")
    # Same channel reused too.
    assert n_channels == 1


# ---------------------------------------------------------------------------
# Mounting smoke check
# ---------------------------------------------------------------------------


def test_chat_route_is_mounted(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    paths = {r.path for r in app.routes}  # type: ignore[attr-defined]
    assert "/v1/chat" in paths


def test_chat_dependency_uses_request_scoped_db(tmp_path: Path) -> None:
    """Sanity check that the chat handler's DB conn is closed after
    the request — i.e. ``Depends(get_db_conn)`` is wired correctly."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.post("/session").mock(
            return_value=Response(200, json=_session_body("ses_q"))
        )
        mock.post("/session/ses_q/message").mock(
            return_value=Response(
                200, json=_assistant_body(msg_id="m", session_id="ses_q")
            )
        )

        with TestClient(app) as client:
            client.post("/v1/chat", json=_channel_payload())

    # If the dep leaked a connection, the next direct connection might
    # contend on the WAL lock indefinitely. Confirm we can take it.
    with connect(settings) as conn:
        assert isinstance(conn, sqlite3.Connection)

"""Tests for ``scufris_server.routes.sessions`` (#10 — ``20260613-091044``).

Three endpoints across two routers:

- ``GET  /v1/sessions``                       — list sessions.
- ``POST /v1/sessions/{channel_id}/clear``    — clear one channel's link.
- ``POST /v1/clear``                          — clear all of a user's links.

Patterns mirror :mod:`tests.unit.test_chat_route` — opencode is
mocked via :mod:`respx`; the SQLite store is real
(:func:`scufris_server.store.connect`) so we exercise the same
WAL / FK / row_factory setup production uses.

Coverage map
------------
- GET happy path: one channel + opencode enriched data → typed
  fields populated, sort order correct.
- GET empty list: valid user, no channels → ``[]``, not 404.
- GET 404 unknown user_id.
- GET opencode-degraded (network error → 200 with nulls; 5xx →
  same).
- GET ordering by ``last_used_at DESC``.
- GET with override active → response scoped to override user
  even when query says otherwise.

- POST clear happy path → ``cleared: true`` + channel row
  preserved.
- POST clear no link (channel exists, link missing) →
  ``cleared: false``, 200 (idempotency).
- POST clear 404 unknown channel.
- POST clear 404 not-owned (different user's channel).

- POST /v1/clear with N>0 links → ``{count: N}``; bob's links
  untouched.
- POST /v1/clear with zero links → ``{count: 0}`` (not 404).
- POST /v1/clear 404 unknown user_id (when no override).
- POST /v1/clear with override → body ``user_id`` ignored.

- Mount smoke check: all three paths in ``app.routes``.
"""

from __future__ import annotations

import time
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
from scufris_server.identity import DEFAULT_USER_ID, DEFAULT_USERNAME
from scufris_server.sessions import create_channel_link
from scufris_server.store import apply_migrations, connect

OPENCODE_TEST_URL = "http://opencode.test"
HEALTH_OK_BODY = {"healthy": True, "version": "1.15.13"}
PROVIDER_OK_BODY: dict[str, Any] = {
    "all": [],
    "default": {"ollama": "qwen3:latest"},
    "connected": ["ollama"],
}


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Default test settings.

    ``overrides`` lets individual tests pin ``user_id`` (the
    ``SCUFRIS_USER_ID`` override) or other knobs without
    re-listing the common state_dir / opencode_url every time.
    """
    return Settings(state_dir=tmp_path, opencode_url=OPENCODE_TEST_URL, **overrides)


def _mock_happy_boot(mock: respx.MockRouter) -> None:
    """Stub the lifespan-required endpoints (health + provider).

    GET /v1/sessions also calls ``GET /session`` for enrichment;
    individual tests register their own mock for that route after
    calling this helper.
    """
    mock.get("/global/health").mock(return_value=Response(200, json=HEALTH_OK_BODY))
    mock.get("/provider").mock(return_value=Response(200, json=PROVIDER_OK_BODY))


def _oc_session_body(
    *,
    id: str = "ses_1",
    title: str | None = "demo session",
    cost: float | None = 0.5,
    tokens_in: int | None = 100,
    tokens_out: int | None = 50,
    created: int = 1_700_000_000_000,
    updated: int = 1_700_000_100_000,
) -> dict[str, Any]:
    """One element of opencode's ``GET /session`` response.

    Includes ``time``, ``tokens``, ``cost``, ``title`` so we can
    verify the enrichment path populates them. Individual tests
    can pass ``title=None`` etc. when probing the
    "opencode-knows-the-session-but-fields-are-sparse" edge.
    """
    body: dict[str, Any] = {
        "id": id,
        "time": {"created": created, "updated": updated},
    }
    if title is not None:
        body["title"] = title
    if cost is not None:
        body["cost"] = cost
    if tokens_in is not None and tokens_out is not None:
        body["tokens"] = {
            "input": tokens_in,
            "output": tokens_out,
            "reasoning": 0,
            "total": tokens_in + tokens_out,
            "cache": {"read": 0, "write": 0},
        }
    return body


def _seed_user(
    settings: Settings,
    user_id: int,
    username: str | None = None,
) -> None:
    """Apply migrations + INSERT a ``users`` row idempotently.

    Most tests need pre-lifespan fixture data (channels keyed by
    ``user_id``); the FK on ``channels.user_id`` makes that
    impossible without a matching ``users`` row first. This
    helper plugs the gap.

    When ``username`` is ``None``:

    - ``user_id == DEFAULT_USER_ID`` → :data:`DEFAULT_USERNAME`.
      Aligns with what the lifespan's ``_seed_default_user``
      would insert, so its ``INSERT OR IGNORE`` no-ops cleanly.
    - otherwise → synthesised ``f"user_{user_id}"``. Won't clash
      with any TOML identity username tests might define
      (those go through the chat-route fixture, not here).

    Idempotent — safe to call repeatedly within a single test or
    across overlapping seed paths.
    """
    if username is None:
        username = DEFAULT_USERNAME if user_id == DEFAULT_USER_ID else f"user_{user_id}"
    with connect(settings) as conn:
        apply_migrations(conn)
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, created_at) VALUES (?, ?, ?)",
            (user_id, username, int(time.time())),
        )
        conn.commit()


def _seed_channel(
    settings: Settings,
    user_id: int,
    *,
    surface: str = "cli",
    surface_id: str = "tty1",
    agent: str = "scufris",
    oc_session_id: str = "ses_test",
    last_used_at: int | None = None,
) -> int:
    """Insert a ``channels`` + ``session_links`` pair for the test.

    Mirrors the chat-route's create path without going through
    HTTP — handy when the unit-under-test is the *list* / *clear*
    surface and we just need fixture data.

    Always seeds ``user_id`` first via :func:`_seed_user` so the
    FK on ``channels.user_id`` is satisfied even when called
    pre-lifespan.

    ``last_used_at`` lets the caller hand-tune the sort key
    (default leaves it at "now").
    """
    _seed_user(settings, user_id)
    with connect(settings) as conn:
        cid = create_channel_link(
            conn, user_id, surface, surface_id, agent, oc_session_id
        )
        if last_used_at is not None:
            with conn:
                conn.execute(
                    "UPDATE session_links SET last_used_at = ? WHERE channel_id = ?",
                    (last_used_at, cid),
                )
    return cid


def _seed_link_only(
    settings: Settings,
    user_id: int,
    *,
    surface: str = "cli",
    surface_id: str = "tty1",
    agent: str = "scufris",
) -> int:
    """Insert a ``channels`` row but no ``session_links``.

    Models the post-clear state where the channel persists but
    its session_link has been dropped — used to drive the
    "POST clear no link → cleared=false" idempotency test.
    """
    _seed_user(settings, user_id)
    with connect(settings) as conn:
        with conn:
            cur = conn.execute(
                "INSERT INTO channels (user_id, surface, surface_id, agent) "
                "VALUES (?, ?, ?, ?)",
                (user_id, surface, surface_id, agent),
            )
            cid = cur.lastrowid
    assert cid is not None
    return cid


# ---------------------------------------------------------------------------
# GET /v1/sessions
# ---------------------------------------------------------------------------


def test_list_sessions_happy_path_enriches_fields(tmp_path: Path) -> None:
    """One linked channel + opencode returning the matching Session
    body → ``title``, ``tokens``, ``cost`` populated from opencode
    while ``channel_id`` / ``oc_session_id`` / ``last_used_at`` come
    from our DB."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    cid = _seed_channel(
        settings,
        user_id=1,
        surface="cli",
        surface_id="tty-1",
        agent="scufris",
        oc_session_id="ses_one",
        last_used_at=1_700_000_500,
    )

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.get("/session").mock(
            return_value=Response(
                200,
                json=[
                    _oc_session_body(
                        id="ses_one",
                        title="hello",
                        cost=1.25,
                        tokens_in=200,
                        tokens_out=80,
                    ),
                    # Extra session opencode knows about that we
                    # never tracked — must be ignored, not surfaced.
                    _oc_session_body(id="ses_other", title="orphan"),
                ],
            )
        )
        with TestClient(app) as client:
            resp = client.get("/v1/sessions")

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1, f"expected only the one tracked channel, got {body!r}"
    item = body[0]
    assert item["channel_id"] == cid
    assert item["channel"] == {
        "surface": "cli",
        "surface_id": "tty-1",
        "agent": "scufris",
    }
    assert item["oc_session_id"] == "ses_one"
    assert item["last_used_at"] == 1_700_000_500
    assert item["title"] == "hello"
    assert item["tokens"] == {"input": 200, "output": 80}
    assert item["cost"] == pytest.approx(1.25)


def test_list_sessions_returns_empty_for_user_with_no_channels(
    tmp_path: Path,
) -> None:
    """Default user, no channels → ``[]`` not 404. Distinct from
    the unknown-user 404 case below."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        # Even with no channels we still call list_sessions; mock
        # an empty list so respx doesn't 404 the request.
        mock.get("/session").mock(return_value=Response(200, json=[]))
        with TestClient(app) as client:
            resp = client.get("/v1/sessions")

    assert resp.status_code == 200
    assert resp.json() == []


def test_list_sessions_404s_when_user_id_does_not_exist(
    tmp_path: Path,
) -> None:
    """``?user_id=9999`` with no override → 404 (D2 — caller-
    supplied ids are validated against the users table)."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            resp = client.get("/v1/sessions", params={"user_id": 9999})

    assert resp.status_code == 404
    assert "9999" in resp.json()["detail"]


def test_list_sessions_degrades_to_nulls_on_opencode_network_error(
    tmp_path: Path,
) -> None:
    """Opencode unreachable mid-request → 200 with channel rows
    intact but ``title`` / ``tokens`` / ``cost`` null. D1 contract:
    the metadata is nice-to-have but the channel ids must remain
    operable so the operator can still clear stale links."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    cid = _seed_channel(
        settings,
        user_id=1,
        surface="cli",
        surface_id="tty-degraded",
        oc_session_id="ses_d",
        last_used_at=1_700_000_700,
    )

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.get("/session").mock(side_effect=httpx.ConnectError("refused"))
        with TestClient(app) as client:
            resp = client.get("/v1/sessions")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    item = body[0]
    assert item["channel_id"] == cid
    assert item["oc_session_id"] == "ses_d"
    assert item["last_used_at"] == 1_700_000_700
    assert item["title"] is None
    assert item["tokens"] is None
    assert item["cost"] is None


def test_list_sessions_degrades_to_nulls_on_opencode_5xx(
    tmp_path: Path,
) -> None:
    """Symmetric to the network-error case — 5xx is also "opencode
    can't help right now" and we degrade rather than 503-ing the
    caller."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    _seed_channel(
        settings,
        user_id=1,
        surface_id="tty-5xx",
        oc_session_id="ses_5xx",
    )

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.get("/session").mock(return_value=Response(503, text="overloaded"))
        with TestClient(app) as client:
            resp = client.get("/v1/sessions")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["title"] is None
    assert body[0]["tokens"] is None
    assert body[0]["cost"] is None


def test_list_sessions_orders_by_last_used_at_desc(tmp_path: Path) -> None:
    """Three channels with hand-tuned ``last_used_at`` come back
    most-recent first. Mirrors the service-layer test in
    test_sessions.py — we're confirming the order survives the
    HTTP round-trip (no sort happening in the route handler)."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    a = _seed_channel(
        settings,
        user_id=1,
        surface_id="a",
        oc_session_id="ses_a",
        last_used_at=1000,
    )
    b = _seed_channel(
        settings,
        user_id=1,
        surface_id="b",
        oc_session_id="ses_b",
        last_used_at=3000,
    )
    c = _seed_channel(
        settings,
        user_id=1,
        surface_id="c",
        oc_session_id="ses_c",
        last_used_at=2000,
    )

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.get("/session").mock(return_value=Response(200, json=[]))
        with TestClient(app) as client:
            resp = client.get("/v1/sessions")

    assert resp.status_code == 200
    ids = [row["channel_id"] for row in resp.json()]
    assert ids == [b, c, a]


def test_list_sessions_override_pins_to_override_user(tmp_path: Path) -> None:
    """``Settings.user_id=2`` (override active) → the response is
    scoped to user 2's channels regardless of any ``?user_id=...``
    query. Even passing the default user id in the query must not
    leak user 1's data."""
    settings = _make_settings(tmp_path, user_id=2)
    _seed_user(settings, 2, "bob")

    # Seed *both* users' channels so we can confirm the right
    # subset comes back.
    bob_channel = _seed_channel(
        settings,
        user_id=2,
        surface_id="bob-tty",
        oc_session_id="ses_bob",
    )
    alex_channel = _seed_channel(
        settings,
        user_id=1,
        surface_id="alex-tty",
        oc_session_id="ses_alex",
    )

    app = create_app(settings)
    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        mock.get("/session").mock(return_value=Response(200, json=[]))
        with TestClient(app) as client:
            # Caller asks for user 1 — override must overrule.
            resp = client.get("/v1/sessions", params={"user_id": 1})

    assert resp.status_code == 200
    ids = [row["channel_id"] for row in resp.json()]
    assert ids == [bob_channel]
    assert alex_channel not in ids


# ---------------------------------------------------------------------------
# POST /v1/sessions/{channel_id}/clear
# ---------------------------------------------------------------------------


def test_clear_session_happy_path(tmp_path: Path) -> None:
    """Channel with a live link → ``cleared: true``; the channels
    row survives (only the link is dropped)."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    cid = _seed_channel(settings, user_id=1, oc_session_id="ses_h")

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            resp = client.post(f"/v1/sessions/{cid}/clear")

    assert resp.status_code == 200
    assert resp.json() == {"cleared": True}

    with connect(settings) as conn:
        # channels row preserved.
        ch = conn.execute(
            "SELECT id, user_id FROM channels WHERE id = ?", (cid,)
        ).fetchone()
        assert ch is not None
        assert ch["user_id"] == 1
        # link gone.
        link = conn.execute(
            "SELECT 1 FROM session_links WHERE channel_id = ?", (cid,)
        ).fetchone()
        assert link is None


def test_clear_session_returns_false_when_no_link(tmp_path: Path) -> None:
    """Channel exists but has no ``session_links`` row (already
    cleared, or fresh post-fork channel) → ``cleared: false``,
    200. Required for the retry idempotency story — see the route
    docstring."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    cid = _seed_link_only(settings, user_id=1, surface_id="tty-empty")

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            resp = client.post(f"/v1/sessions/{cid}/clear")

    assert resp.status_code == 200
    assert resp.json() == {"cleared": False}


def test_clear_session_404s_for_unknown_channel(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            resp = client.post("/v1/sessions/9999/clear")

    assert resp.status_code == 404
    assert "9999" in resp.json()["detail"]


def test_clear_session_404s_when_channel_belongs_to_other_user(
    tmp_path: Path,
) -> None:
    """alex (default user) tries to clear bob's channel → 404 with
    the same body as "channel doesn't exist". D2 — the response
    must not leak that the channel exists for someone else."""
    settings = _make_settings(tmp_path)
    _seed_user(settings, 2, "bob")
    bob_channel = _seed_channel(settings, user_id=2, oc_session_id="ses_bob_owned")

    app = create_app(settings)
    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            # Default principal = alex (user 1). bob_channel is user 2's.
            resp = client.post(f"/v1/sessions/{bob_channel}/clear")

    assert resp.status_code == 404
    # Same message as "channel doesn't exist" — no leak.
    assert str(bob_channel) in resp.json()["detail"]

    # bob's link is still alive.
    with connect(settings) as conn:
        link = conn.execute(
            "SELECT 1 FROM session_links WHERE channel_id = ?", (bob_channel,)
        ).fetchone()
        assert link is not None


# ---------------------------------------------------------------------------
# POST /v1/clear
# ---------------------------------------------------------------------------


def test_bulk_clear_drops_all_user_links_and_preserves_channels(
    tmp_path: Path,
) -> None:
    """N>0 case — clear_user_links returns the count of removed
    rows. Channels rows survive on both sides; bob's link is
    untouched (proves the WHERE clause scopes correctly)."""
    settings = _make_settings(tmp_path)
    _seed_user(settings, 2, "bob")

    alex_a = _seed_channel(
        settings, user_id=1, surface_id="alex-a", oc_session_id="ses_alex_a"
    )
    alex_b = _seed_channel(
        settings, user_id=1, surface_id="alex-b", oc_session_id="ses_alex_b"
    )
    bob_one = _seed_channel(
        settings, user_id=2, surface_id="bob-one", oc_session_id="ses_bob_one"
    )

    app = create_app(settings)
    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            resp = client.post("/v1/clear", json={"user_id": 1})

    assert resp.status_code == 200
    assert resp.json() == {"count": 2}

    with connect(settings) as conn:
        # alex's links are gone.
        alex_links = conn.execute(
            "SELECT COUNT(*) AS n FROM session_links WHERE channel_id IN (?, ?)",
            (alex_a, alex_b),
        ).fetchone()["n"]
        assert alex_links == 0
        # alex's channel rows survive (ADR-13 — channels are sticky).
        alex_channels = conn.execute(
            "SELECT COUNT(*) AS n FROM channels WHERE user_id = 1"
        ).fetchone()["n"]
        assert alex_channels == 2
        # bob's link untouched.
        bob_link = conn.execute(
            "SELECT oc_session_id FROM session_links WHERE channel_id = ?",
            (bob_one,),
        ).fetchone()
        assert bob_link is not None
        assert bob_link["oc_session_id"] == "ses_bob_one"


def test_bulk_clear_returns_zero_when_user_has_no_links(tmp_path: Path) -> None:
    """User exists but has no ``session_links`` → 200 with
    ``count: 0``. Must NOT 404 — the operator's intent ("there
    should be no live links for this user") is already satisfied
    (D6)."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            resp = client.post("/v1/clear", json={"user_id": 1})

    assert resp.status_code == 200
    assert resp.json() == {"count": 0}


def test_bulk_clear_404s_for_unknown_user_id(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            resp = client.post("/v1/clear", json={"user_id": 9999})

    assert resp.status_code == 404
    assert "9999" in resp.json()["detail"]


def test_bulk_clear_rejects_non_positive_user_id(tmp_path: Path) -> None:
    """``user_id=0`` violates the ``Field(ge=1)`` constraint →
    pydantic 422, never reaches our handler. Defensive — the
    constraint protects against accidental "delete-everything"
    queries hitting the wrong row."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            resp = client.post("/v1/clear", json={"user_id": 0})

    assert resp.status_code == 422


def test_bulk_clear_override_overrides_body_user_id(tmp_path: Path) -> None:
    """Override pinned to user 1 + body asking to clear user 2 →
    the override silently wins, user 1's links are cleared, user 2
    is untouched. Confirms the override-first authz rule (D2)
    can't be circumvented by a hostile body."""
    settings = _make_settings(tmp_path, user_id=1)
    _seed_user(settings, 2, "bob")

    alex_one = _seed_channel(
        settings, user_id=1, surface_id="alex-only", oc_session_id="ses_alex"
    )
    bob_one = _seed_channel(
        settings, user_id=2, surface_id="bob-only", oc_session_id="ses_bob"
    )

    app = create_app(settings)
    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            resp = client.post("/v1/clear", json={"user_id": 2})

    assert resp.status_code == 200
    # Override forced principal=1, so we cleared alex's one link.
    assert resp.json() == {"count": 1}

    with connect(settings) as conn:
        alex_link = conn.execute(
            "SELECT 1 FROM session_links WHERE channel_id = ?", (alex_one,)
        ).fetchone()
        assert alex_link is None
        bob_link = conn.execute(
            "SELECT 1 FROM session_links WHERE channel_id = ?", (bob_one,)
        ).fetchone()
        assert bob_link is not None


# ---------------------------------------------------------------------------
# Mounting smoke check
# ---------------------------------------------------------------------------


def test_session_routes_are_mounted(tmp_path: Path) -> None:
    """Both routers (``sessions_router`` and ``clear_router``) are
    in ``app.routes``. Trip-wire for step 10 — if a future refactor
    drops one from ``ROUTERS``, this fails before the integration
    suite catches it."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    paths = {r.path for r in app.routes}  # type: ignore[attr-defined]
    assert "/v1/sessions" in paths
    assert "/v1/sessions/{channel_id}/clear" in paths
    assert "/v1/clear" in paths

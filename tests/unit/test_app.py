"""Tests for ``scufris_server.app`` factory + lifespan (step 5 of #9)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from scufris_server import __version__
from scufris_server.app import create_app
from scufris_server.config import Settings, get_settings
from scufris_server.identity import IdentityFile
from scufris_server.store import connect

OPENCODE_TEST_URL = "http://opencode.test"
HEALTH_OK_BODY = {"healthy": True, "version": "1.15.13"}
PROVIDER_OK_BODY: dict[str, Any] = {
    "all": [],
    "default": {"ollama": "qwen3:latest"},
    "connected": ["ollama"],
}


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Stop a cached singleton from previous tests leaking into the app."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_settings(tmp_path: Path, url: str = OPENCODE_TEST_URL) -> Settings:
    return Settings(state_dir=tmp_path, opencode_url=url)


def _mock_happy_opencode(mock: respx.MockRouter) -> None:
    """Register the standard set of happy-boot mocks: health + provider."""
    mock.get("/global/health").mock(return_value=Response(200, json=HEALTH_OK_BODY))
    mock.get("/provider").mock(return_value=Response(200, json=PROVIDER_OK_BODY))


# ---------------------------------------------------------------------------
# Happy boot
# ---------------------------------------------------------------------------


def test_lifespan_happy_boot_migrates_and_probes_health(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        with TestClient(app):
            # Settings stashed for later dependency lookups.
            assert app.state.settings is settings

            # Probe succeeded.
            health = app.state.opencode_initial_health
            assert health is not None
            assert health.healthy is True
            assert health.version == "1.15.13"

            # Migrations applied: DB file + schema exist.
            assert (tmp_path / "scufris.sqlite").is_file()
            with connect(settings) as conn:
                tables = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            assert "users" in tables
            assert "_schema_migrations" in tables

            # Client attached and live.
            assert app.state.opencode is not None
            assert not app.state.opencode._client.is_closed

    # Shutdown ran — client closed.
    assert app.state.opencode._client.is_closed


def test_lifespan_seeds_default_user_idempotently(tmp_path: Path) -> None:
    """``users(id=1)`` is seeded once and stays stable across boots."""
    settings = _make_settings(tmp_path)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        # First boot.
        app1 = create_app(settings)
        with TestClient(app1):
            with connect(settings) as conn:
                rows = [
                    (r["id"], r["username"])
                    for r in conn.execute("SELECT id, username FROM users")
                ]
            assert rows == [(1, "default")]

        # Second boot reusing the same DB — should not duplicate.
        app2 = create_app(settings)
        with TestClient(app2):
            with connect(settings) as conn:
                rows = [
                    (r["id"], r["username"])
                    for r in conn.execute("SELECT id, username FROM users ORDER BY id")
                ]
            assert rows == [(1, "default")]


def test_lifespan_caches_default_model_after_health_success(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        with TestClient(app):
            ref = app.state.opencode_default_model
            assert ref is not None
            assert ref.providerID == "ollama"
            assert ref.modelID == "qwen3:latest"


def test_lifespan_default_model_probe_failure_leaves_cache_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Health succeeds; /provider fails. App still boots; chat will 503."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(return_value=Response(200, json=HEALTH_OK_BODY))
        mock.get("/provider").mock(return_value=Response(503, text="overloaded"))
        with caplog.at_level("WARNING", logger="scufris_server"):
            with TestClient(app):
                assert app.state.opencode_default_model is None

    assert any(
        "default-model probe failed" in r.getMessage() for r in caplog.records
    ), f"expected probe-failed warning, got: {[r.getMessage() for r in caplog.records]}"


def test_lifespan_default_model_none_when_no_connected_provider(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """opencode is up but has nothing connected — chat will 503."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(return_value=Response(200, json=HEALTH_OK_BODY))
        mock.get("/provider").mock(
            return_value=Response(200, json={"all": [], "default": {}, "connected": []})
        )
        with caplog.at_level("WARNING", logger="scufris_server"):
            with TestClient(app):
                assert app.state.opencode_default_model is None

    assert any("no connected provider" in r.getMessage() for r in caplog.records), (
        f"expected no-connected warning, got: {[r.getMessage() for r in caplog.records]}"
    )


def test_lifespan_skips_default_model_probe_on_degraded_boot(tmp_path: Path) -> None:
    """When health fails we don't even try /provider — same upstream wall."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        health_route = mock.get("/global/health").mock(
            side_effect=httpx.ConnectError("refused")
        )
        provider_route = mock.get("/provider").mock(
            return_value=Response(200, json=PROVIDER_OK_BODY)
        )
        with TestClient(app):
            assert app.state.opencode_initial_health is None
            assert app.state.opencode_default_model is None

    assert health_route.called
    assert not provider_route.called, "should not have probed /provider"


# ---------------------------------------------------------------------------
# Event bus lifecycle (#11 step 7)
# ---------------------------------------------------------------------------


def test_lifespan_attaches_event_bus_to_state(tmp_path: Path) -> None:
    """``app.state.opencode_event_bus`` is an :class:`EventBus` instance
    after lifespan startup. Step 8 (chat_stream) consumes it via
    :func:`get_event_bus`.
    """
    from scufris_server.events import EventBus

    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        with TestClient(app):
            bus = app.state.opencode_event_bus
            assert isinstance(bus, EventBus)
            # ``start`` was called — the reader task exists.
            assert bus._task is not None
            assert not bus._task.done()


def test_lifespan_stops_event_bus_before_closing_client(tmp_path: Path) -> None:
    """Shutdown ordering: bus stops first, then client closes. Verified
    by checking both states post-shutdown.

    Why the order matters: the bus borrows the client's httpx
    transport. If the client closed first, the bus's in-flight
    ``GET /event`` would surface as an ugly ``ClientClosedError``
    in the reconnect loop's logs. Stopping the bus first cancels
    its reader cleanly.
    """
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        with TestClient(app):
            pass  # exit → lifespan shutdown

    bus = app.state.opencode_event_bus
    assert bus._task is None, "bus.stop() should clear _task"
    assert bus.connected is False
    assert app.state.opencode._client.is_closed, "client should also be closed"


def test_lifespan_starts_event_bus_even_on_degraded_boot(tmp_path: Path) -> None:
    """When opencode is unreachable, the bus is still started — its
    reconnect loop will keep retrying in the background until
    opencode comes back. The streaming chat handler's
    ``bus.subscribe()`` call will raise :class:`OpencodeUnavailable`
    after its per-call timeout, which is the user-visible surface
    for upstream death — not a startup failure.

    This validates TASK.md's "tolerate ``OpencodeNetworkError`` on
    first connect" requirement: the lifespan never re-raises.
    """
    from scufris_server.events import EventBus

    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(side_effect=httpx.ConnectError("refused"))
        with TestClient(app):
            assert app.state.opencode_initial_health is None  # degraded
            bus = app.state.opencode_event_bus
            assert isinstance(bus, EventBus)
            assert bus._task is not None  # started anyway
            assert not bus._task.done()


# ---------------------------------------------------------------------------
# Degraded boot
# ---------------------------------------------------------------------------


def test_lifespan_boots_in_degraded_mode_when_opencode_unreachable(
    tmp_path: Path,
) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with TestClient(app):
            # Lifespan completed despite probe failure.
            assert app.state.opencode_initial_health is None
            # Client still attached so callers can retry.
            assert app.state.opencode is not None
            # Migrations still ran.
            assert (tmp_path / "scufris.sqlite").is_file()

    # Shutdown still closes the client cleanly.
    assert app.state.opencode._client.is_closed


def test_lifespan_boots_in_degraded_mode_when_opencode_returns_503(
    tmp_path: Path,
) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(return_value=Response(503, text="overloaded"))
        with TestClient(app):
            assert app.state.opencode_initial_health is None


def test_lifespan_logs_warning_on_degraded_boot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(side_effect=httpx.ConnectError("refused"))
        with caplog.at_level("WARNING", logger="scufris_server"):
            with TestClient(app):
                pass

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("opencode unreachable" in r.getMessage() for r in warnings), (
        f"expected degraded-boot warning, got: {[r.getMessage() for r in warnings]}"
    )


# ---------------------------------------------------------------------------
# Identity (#12)
# ---------------------------------------------------------------------------


def _write_config_toml(path: Path, body: str) -> Path:
    """Write ``body`` to ``path``, creating parents. Returns the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_lifespan_loads_empty_identity_when_config_file_missing(
    tmp_path: Path,
) -> None:
    """``app.state.user_identity`` is always populated.

    Pointing ``config_path`` at a non-existent file must yield an
    empty :class:`IdentityFile` (``user=None``) rather than raising —
    that's the contract :func:`identity.load_user_identity` documents.
    """
    settings = Settings(
        state_dir=tmp_path,
        opencode_url=OPENCODE_TEST_URL,
        config_path=tmp_path / "nonexistent.toml",
    )
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        with TestClient(app):
            identity = app.state.user_identity
            assert isinstance(identity, IdentityFile)
            assert identity.user is None


def test_lifespan_loads_identity_from_explicit_config_path(
    tmp_path: Path,
) -> None:
    """``Settings.config_path`` overrides the XDG default and the file
    contents are parsed into ``app.state.user_identity``."""
    config = _write_config_toml(
        tmp_path / "config.toml",
        # Single-user shape (v1 carryover, design §11).
        "[user]\n"
        'username = "alex"\n'
        "[user.identity]\n"
        'cli = "alex"\n'
        'telegram = "8231376426"\n',
    )
    settings = Settings(
        state_dir=tmp_path,
        opencode_url=OPENCODE_TEST_URL,
        config_path=config,
    )
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        with TestClient(app):
            identity = app.state.user_identity
            assert isinstance(identity, IdentityFile)
            assert identity.user is not None
            assert identity.user.username == "alex"
            assert identity.user.identity == {
                "cli": "alex",
                "telegram": "8231376426",
            }


def test_lifespan_falls_back_to_xdg_config_home_when_config_path_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``Settings.config_path`` is ``None``, the lifespan defers
    to :func:`identity._xdg_config_path` which honours
    ``$XDG_CONFIG_HOME``."""
    xdg = tmp_path / "xdg"
    _write_config_toml(
        xdg / "scufris" / "config.toml",
        '[user]\nusername = "xdg-user"\n',
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    settings = Settings(state_dir=tmp_path, opencode_url=OPENCODE_TEST_URL)
    # Sanity: nothing in Settings is pinning the path; we're really
    # exercising the XDG fallback.
    assert settings.config_path is None

    app = create_app(settings)
    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        with TestClient(app):
            identity = app.state.user_identity
            assert identity.user is not None
            assert identity.user.username == "xdg-user"


def test_lifespan_identity_override_is_none_by_default(tmp_path: Path) -> None:
    """No ``SCUFRIS_USER_ID`` set → no override; ``resolve_user`` will
    fall through to TOML / default-user resolution."""
    settings = _make_settings(tmp_path)
    assert settings.user_id is None  # precondition
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        with TestClient(app):
            assert app.state.identity_override is None


def test_lifespan_caches_identity_override_when_user_exists(
    tmp_path: Path,
) -> None:
    """``user_id=1`` matches the seeded default user, so validation
    succeeds and the override is cached on ``app.state``."""
    settings = Settings(
        state_dir=tmp_path,
        opencode_url=OPENCODE_TEST_URL,
        user_id=1,
    )
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        with TestClient(app):
            assert app.state.identity_override == 1


def test_lifespan_raises_when_identity_override_user_missing(
    tmp_path: Path,
) -> None:
    """``SCUFRIS_USER_ID=999`` with no such user must blow up at boot.

    Discovering the misconfig on the first chat request would be
    silent corruption (chat would 500 with a stale FK error). The
    fast crash is documented in :func:`app._validate_identity_override`.
    """
    settings = Settings(
        state_dir=tmp_path,
        opencode_url=OPENCODE_TEST_URL,
        user_id=999,
    )
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        with pytest.raises(RuntimeError, match="SCUFRIS_USER_ID=999"):
            with TestClient(app):
                pass


# ---------------------------------------------------------------------------
# Factory behavior
# ---------------------------------------------------------------------------


def test_create_app_uses_default_settings_when_none_provided(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SCUFRIS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("OPENCODE_URL", "http://stub.opencode.test")
    # No TestClient — we only need to verify settings binding, not run
    # the lifespan (which would try to probe stub.opencode.test for real).
    app = create_app()
    assert app.state.settings.state_dir == tmp_path
    assert app.state.settings.opencode_url == "http://stub.opencode.test"


def test_create_app_mounts_routers_from_routes_package(tmp_path: Path) -> None:
    """The factory walks ``ROUTERS`` and includes each. Spot-check by
    asserting a known path from step 7's health router is mounted."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    routes_paths = {r.path for r in app.routes}  # type: ignore[attr-defined]
    # Built-in OpenAPI endpoints.
    assert "/openapi.json" in routes_paths
    assert "/docs" in routes_paths
    # From scufris_server.routes.health (step 7).
    assert "/v1/healthz" in routes_paths
    assert "/v1/version" in routes_paths


def test_openapi_metadata_matches_package(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_opencode(mock)
        with TestClient(app) as test_client:
            resp = test_client.get("/openapi.json")

    assert resp.status_code == 200
    meta = resp.json()["info"]
    assert meta["title"] == "scufris-server"
    assert meta["version"] == __version__
    assert "Scufris daemon" in meta["description"]

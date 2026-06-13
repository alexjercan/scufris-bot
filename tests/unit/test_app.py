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

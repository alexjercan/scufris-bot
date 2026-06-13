"""Tests for ``scufris_server.app`` factory + lifespan (step 5 of #9)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

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


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Stop a cached singleton from previous tests leaking into the app."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_settings(tmp_path: Path, url: str = OPENCODE_TEST_URL) -> Settings:
    return Settings(state_dir=tmp_path, opencode_url=url)


# ---------------------------------------------------------------------------
# Happy boot
# ---------------------------------------------------------------------------


def test_lifespan_happy_boot_migrates_and_probes_health(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(return_value=Response(200, json=HEALTH_OK_BODY))
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
    """Mount loop runs over ``ROUTERS``; empty in step 5 but the path
    must exist so step 7+ can append without touching the factory."""
    from scufris_server import routes

    settings = _make_settings(tmp_path)
    app = create_app(settings)

    # Built-in OpenAPI endpoints only — no user routes yet.
    routes_paths = {r.path for r in app.routes}  # type: ignore[attr-defined]
    assert "/openapi.json" in routes_paths
    assert "/docs" in routes_paths

    # ROUTERS list is the wiring surface.
    assert routes.ROUTERS == []


def test_openapi_metadata_matches_package(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(return_value=Response(200, json=HEALTH_OK_BODY))
        with TestClient(app) as test_client:
            resp = test_client.get("/openapi.json")

    assert resp.status_code == 200
    meta = resp.json()["info"]
    assert meta["title"] == "scufris-server"
    assert meta["version"] == __version__
    assert "Scufris daemon" in meta["description"]

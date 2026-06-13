"""Tests for ``scufris_server.routes.health`` (step 7 of #9)."""

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

OPENCODE_TEST_URL = "http://opencode.test"
HEALTH_OK_BODY = {"healthy": True, "version": "1.15.13"}


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(state_dir=tmp_path, opencode_url=OPENCODE_TEST_URL)


# ---------------------------------------------------------------------------
# /v1/healthz
# ---------------------------------------------------------------------------


def test_healthz_reports_ok_when_opencode_is_up(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(return_value=Response(200, json=HEALTH_OK_BODY))
        with TestClient(app) as client:
            resp = client.get("/v1/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["opencode"] == {"healthy": True, "version": "1.15.13"}


def test_healthz_returns_200_with_error_when_opencode_unreachable(
    tmp_path: Path,
) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(side_effect=httpx.ConnectError("refused"))
        with TestClient(app) as client:
            resp = client.get("/v1/healthz")

    # Still 200 — opencode failure is reported as data, not as HTTP status.
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "error" in body["opencode"]
    assert body["opencode"]["error"]  # non-empty


def test_healthz_returns_200_with_error_when_opencode_returns_503(
    tmp_path: Path,
) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(return_value=Response(503, text="overloaded"))
        with TestClient(app) as client:
            resp = client.get("/v1/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "error" in body["opencode"]


def test_healthz_refreshes_version_cache(tmp_path: Path) -> None:
    """A successful /v1/healthz should populate app.state.opencode_version
    so subsequent /v1/version calls hit the cache."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        # Boot probe + /v1/healthz call both consume the response.
        mock.get("/global/health").mock(return_value=Response(200, json=HEALTH_OK_BODY))
        with TestClient(app) as client:
            client.get("/v1/healthz")
            assert app.state.opencode_version == "1.15.13"


def test_healthz_includes_request_id_header(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(return_value=Response(200, json=HEALTH_OK_BODY))
        with TestClient(app) as client:
            resp = client.get("/v1/healthz")

    rid = resp.headers.get("x-request-id")
    assert rid is not None
    assert len(rid) == 26  # ULID


def test_healthz_logs_info_on_success(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(return_value=Response(200, json=HEALTH_OK_BODY))
        with caplog.at_level("INFO", logger="scufris_server"):
            with TestClient(app) as client:
                client.get("/v1/healthz")

    matching = [
        r
        for r in caplog.records
        if r.name == "scufris_server.routes.health"
        and r.levelname == "INFO"
        and "healthz ok" in r.getMessage()
    ]
    assert matching, f"expected healthz-ok INFO record, got: {caplog.records}"
    record = matching[0]
    assert getattr(record, "opencode_version", None) == "1.15.13"
    assert getattr(record, "opencode_healthy", None) is True


def test_healthz_logs_warning_on_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(side_effect=httpx.ConnectError("refused"))
        with caplog.at_level("WARNING", logger="scufris_server"):
            with TestClient(app) as client:
                client.get("/v1/healthz")

    matching = [
        r
        for r in caplog.records
        if r.name == "scufris_server.routes.health"
        and r.levelname == "WARNING"
        and "healthz degraded" in r.getMessage()
    ]
    assert matching, f"expected healthz-degraded WARNING, got: {caplog.records}"
    assert getattr(matching[0], "error", "")  # non-empty error string


# ---------------------------------------------------------------------------
# /v1/version
# ---------------------------------------------------------------------------


def test_version_returns_scufris_version_always(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(return_value=Response(200, json=HEALTH_OK_BODY))
        with TestClient(app) as client:
            resp = client.get("/v1/version")

    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == __version__


def test_version_uses_cache_filled_by_boot_probe(tmp_path: Path) -> None:
    """When the lifespan probe succeeded, /v1/version should serve from
    cache without making another HTTP call to opencode."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        boot_route = mock.get("/global/health").mock(
            return_value=Response(200, json=HEALTH_OK_BODY)
        )
        with TestClient(app) as client:
            assert boot_route.call_count == 1  # boot probe
            resp = client.get("/v1/version")
            # No additional probe — cache hit.
            assert boot_route.call_count == 1

    assert resp.json()["opencode_version"] == "1.15.13"


def test_version_probes_lazily_on_cache_miss(tmp_path: Path) -> None:
    """Boot probe failed (degraded boot) → cache empty → /v1/version
    triggers a fresh probe."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        # Boot probe fails, then succeeds on the lazy retry.
        mock.get("/global/health").mock(
            side_effect=[
                httpx.ConnectError("refused"),
                Response(200, json=HEALTH_OK_BODY),
            ]
        )
        with TestClient(app) as client:
            assert app.state.opencode_version is None  # boot probe failed
            resp = client.get("/v1/version")

    assert resp.json()["opencode_version"] == "1.15.13"


def test_version_returns_null_when_probe_fails_and_does_not_cache(
    tmp_path: Path,
) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        # Both boot probe and lazy probe fail.
        mock.get("/global/health").mock(side_effect=httpx.ConnectError("refused"))
        with TestClient(app) as client:
            resp = client.get("/v1/version")
            # Cache stays empty — next call should re-probe.
            assert app.state.opencode_version is None

    body = resp.json()
    assert body["version"] == __version__
    assert body["opencode_version"] is None


def test_version_cache_persists_across_calls(tmp_path: Path) -> None:
    """Once the cache is populated, repeated calls don't re-probe."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        boot_route = mock.get("/global/health").mock(
            return_value=Response(200, json=HEALTH_OK_BODY)
        )
        with TestClient(app) as client:
            client.get("/v1/version")  # cache hit (filled by boot probe)
            client.get("/v1/version")  # still cache hit
            client.get("/v1/version")  # still cache hit

    # Only the boot probe ever went out.
    assert boot_route.call_count == 1


def test_version_logs_info_on_cache_hit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(return_value=Response(200, json=HEALTH_OK_BODY))
        with TestClient(app) as client:
            # Boot probe fills the cache; clear records so we only see the
            # /v1/version log line.
            caplog.clear()
            with caplog.at_level("INFO", logger="scufris_server"):
                client.get("/v1/version")

    matching = [
        r
        for r in caplog.records
        if r.name == "scufris_server.routes.health"
        and r.levelname == "INFO"
        and "version cache hit" in r.getMessage()
    ]
    assert matching, f"expected version cache-hit INFO, got: {caplog.records}"
    record = matching[0]
    assert getattr(record, "cached", None) is True
    assert getattr(record, "opencode_version", None) == "1.15.13"


def test_version_logs_info_on_cache_miss_success(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        # Boot probe fails → cache miss → lazy probe succeeds.
        mock.get("/global/health").mock(
            side_effect=[
                httpx.ConnectError("refused"),
                Response(200, json=HEALTH_OK_BODY),
            ]
        )
        with TestClient(app) as client:
            caplog.clear()
            with caplog.at_level("INFO", logger="scufris_server"):
                client.get("/v1/version")

    matching = [
        r
        for r in caplog.records
        if r.name == "scufris_server.routes.health"
        and r.levelname == "INFO"
        and "version cache populated" in r.getMessage()
    ]
    assert matching, f"expected version cache-populated INFO, got: {caplog.records}"
    record = matching[0]
    assert getattr(record, "cached", None) is False
    assert getattr(record, "opencode_version", None) == "1.15.13"


def test_version_logs_warning_on_probe_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        mock.get("/global/health").mock(side_effect=httpx.ConnectError("refused"))
        with TestClient(app) as client:
            caplog.clear()
            with caplog.at_level("WARNING", logger="scufris_server"):
                client.get("/v1/version")

    matching = [
        r
        for r in caplog.records
        if r.name == "scufris_server.routes.health"
        and r.levelname == "WARNING"
        and "version probe failed" in r.getMessage()
    ]
    assert matching, f"expected version probe-failed WARNING, got: {caplog.records}"
    assert getattr(matching[0], "error", "")  # non-empty error string

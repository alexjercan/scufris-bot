"""Tests for ``scufris_server.routes.identity`` (#12 — ``20260613-091046``).

Covers all four resolution paths via the HTTP layer:

1. **Default fallback** — empty TOML, unknown ``surface_id`` → user_id=1,
   ``surface_bindings`` row materialised.
2. **TOML hit** — ``[user.identity]`` mapping matches → user row created,
   binding row materialised.
3. **Override (D4)** — ``SCUFRIS_USER_ID`` set → response pinned to that
   user; binding row *not* written (override skips materialisation).
4. **Cache hit** — second call with same key → same user_id, no
   duplicate binding row (idempotency).

Plus aggregation (``bound_surfaces`` lists every binding sorted), the
422 validation surface (empty surface / surface_id, missing fields),
and a mount smoke check.

Pure unit: opencode is mocked via :mod:`respx` so the lifespan can
boot through health + provider probes. SQLite is real (one file per
test, ``tmp_path``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

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


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_settings(
    tmp_path: Path,
    *,
    config_path: Path | None = None,
    user_id: int | None = None,
) -> Settings:
    """Build a :class:`Settings` for tests.

    ``config_path`` and ``user_id`` are passed through verbatim so each
    case can pin them independently. State and opencode URL stay fixed.
    """
    return Settings(
        state_dir=tmp_path,
        opencode_url=OPENCODE_TEST_URL,
        config_path=config_path,
        user_id=user_id,
    )


def _mock_happy_boot(mock: respx.MockRouter) -> None:
    """Stub the lifespan-required endpoints (health + provider).

    The identity route doesn't talk to opencode, but the lifespan
    still probes during ``TestClient.__enter__``; without these the
    boot logs a degraded-mode warning (harmless for our assertions
    but noisy in the captured logs).
    """
    mock.get("/global/health").mock(return_value=Response(200, json=HEALTH_OK_BODY))
    mock.get("/provider").mock(return_value=Response(200, json=PROVIDER_OK_BODY))


def _write_config_toml(path: Path, body: str) -> Path:
    """Write ``body`` to ``path`` with parents created. Returns ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Default fallback (no TOML, unknown surface_id)
# ---------------------------------------------------------------------------


def test_resolve_default_fallback_returns_default_user_and_writes_binding(
    tmp_path: Path,
) -> None:
    """With no ``config.toml`` and no override, every ``(surface,
    surface_id)`` resolves to user_id=1 (the seeded default user).
    The binding row gets materialised so subsequent calls are cache
    hits."""
    settings = _make_settings(tmp_path, config_path=tmp_path / "missing.toml")
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            resp = client.post(
                "/v1/identity/resolve",
                json={"surface": "cli", "surface_id": "unknown"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == 1
    assert body["username"] == "default"
    assert body["surface"] == "cli"
    assert body["surface_id"] == "unknown"
    # First contact materialises the binding.
    assert body["bound_surfaces"] == [
        {"surface": "cli", "surface_id": "unknown"},
    ]

    with connect(settings) as conn:
        rows = list(
            conn.execute(
                "SELECT user_id, surface, surface_id FROM surface_bindings"
            )
        )
    assert len(rows) == 1
    assert (rows[0]["user_id"], rows[0]["surface"], rows[0]["surface_id"]) == (
        1,
        "cli",
        "unknown",
    )


# ---------------------------------------------------------------------------
# TOML hit (D2 + D3): config maps surface → surface_id
# ---------------------------------------------------------------------------


def test_resolve_toml_hit_creates_user_and_binding(tmp_path: Path) -> None:
    """``[user] username="alex" [user.identity] cli="alex"`` plus a
    request for ``(cli, alex)`` should:
    - insert a new ``users`` row for "alex" (id != 1),
    - insert a ``surface_bindings`` row pointing at that id,
    - return both in the response."""
    config = _write_config_toml(
        tmp_path / "config.toml",
        '[user]\n'
        'username = "alex"\n'
        '[user.identity]\n'
        'cli = "alex"\n',
    )
    settings = _make_settings(tmp_path, config_path=config)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            resp = client.post(
                "/v1/identity/resolve",
                json={"surface": "cli", "surface_id": "alex"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "alex"
    # New user; not the seeded default.
    assert body["user_id"] != 1
    assert body["surface"] == "cli"
    assert body["surface_id"] == "alex"
    assert body["bound_surfaces"] == [
        {"surface": "cli", "surface_id": "alex"},
    ]

    alex_id = body["user_id"]
    with connect(settings) as conn:
        users = {
            r["username"]: r["id"]
            for r in conn.execute("SELECT id, username FROM users")
        }
        bindings = list(
            conn.execute(
                "SELECT user_id, surface, surface_id FROM surface_bindings"
            )
        )
    # Both default and alex exist.
    assert users == {"default": 1, "alex": alex_id}
    assert len(bindings) == 1
    assert (
        bindings[0]["user_id"],
        bindings[0]["surface"],
        bindings[0]["surface_id"],
    ) == (alex_id, "cli", "alex")


# ---------------------------------------------------------------------------
# Override (D4): SCUFRIS_USER_ID short-circuits resolution
# ---------------------------------------------------------------------------


def test_resolve_override_pins_to_user_id_without_writing_binding(
    tmp_path: Path,
) -> None:
    """``Settings(user_id=1)`` is the server-side override (D4). Every
    resolve returns user_id=1, and *no* ``surface_bindings`` row is
    written — the override is a deliberate detour around the binding
    table so dev/test deployments stay clean."""
    # TOML with a different mapping; override must dominate.
    config = _write_config_toml(
        tmp_path / "config.toml",
        '[user]\n'
        'username = "alex"\n'
        '[user.identity]\n'
        'cli = "alex"\n',
    )
    settings = _make_settings(tmp_path, config_path=config, user_id=1)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            # Request something that *would* TOML-hit if not for override.
            resp = client.post(
                "/v1/identity/resolve",
                json={"surface": "cli", "surface_id": "alex"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == 1
    assert body["username"] == "default"
    assert body["surface"] == "cli"
    assert body["surface_id"] == "alex"
    # No binding rows were written — override skips materialisation.
    assert body["bound_surfaces"] == []

    with connect(settings) as conn:
        n_bindings = conn.execute(
            "SELECT COUNT(*) AS n FROM surface_bindings"
        ).fetchone()["n"]
        # The TOML-named user is *also* not auto-created by the override
        # path; only the seeded default exists.
        usernames = {
            r["username"]
            for r in conn.execute("SELECT username FROM users")
        }
    assert n_bindings == 0
    assert usernames == {"default"}


# ---------------------------------------------------------------------------
# Idempotency + bound_surfaces aggregation
# ---------------------------------------------------------------------------


def test_resolve_is_idempotent_for_same_surface_and_surface_id(
    tmp_path: Path,
) -> None:
    """Two resolve calls with the same key must return the same
    user_id and leave exactly one ``surface_bindings`` row. The
    second call hits the binding cache (step 2 of the algorithm)."""
    settings = _make_settings(tmp_path, config_path=tmp_path / "missing.toml")
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            r1 = client.post(
                "/v1/identity/resolve",
                json={"surface": "cli", "surface_id": "term-1"},
            )
            r2 = client.post(
                "/v1/identity/resolve",
                json={"surface": "cli", "surface_id": "term-1"},
            )

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["user_id"] == r2.json()["user_id"]
    assert r1.json()["bound_surfaces"] == r2.json()["bound_surfaces"]

    with connect(settings) as conn:
        rows = list(
            conn.execute(
                "SELECT surface, surface_id FROM surface_bindings"
            )
        )
    assert len(rows) == 1


def test_resolve_aggregates_bound_surfaces_across_calls(tmp_path: Path) -> None:
    """``bound_surfaces`` lists every binding for the resolved user,
    sorted ``(surface ASC, surface_id ASC)``. After binding both a
    cli and a telegram surface to the TOML user, both should appear
    in subsequent responses regardless of which key triggered them."""
    config = _write_config_toml(
        tmp_path / "config.toml",
        '[user]\n'
        'username = "alex"\n'
        '[user.identity]\n'
        'cli = "alex"\n'
        'telegram = "8231376426"\n',
    )
    settings = _make_settings(tmp_path, config_path=config)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            r1 = client.post(
                "/v1/identity/resolve",
                json={"surface": "cli", "surface_id": "alex"},
            )
            r2 = client.post(
                "/v1/identity/resolve",
                json={"surface": "telegram", "surface_id": "8231376426"},
            )

    # First call only sees its own binding.
    assert r1.json()["bound_surfaces"] == [
        {"surface": "cli", "surface_id": "alex"},
    ]
    # Second call sees both, sorted.
    assert r2.json()["bound_surfaces"] == [
        {"surface": "cli", "surface_id": "alex"},
        {"surface": "telegram", "surface_id": "8231376426"},
    ]
    # Same user across both calls.
    assert r1.json()["user_id"] == r2.json()["user_id"]


# ---------------------------------------------------------------------------
# Validation: pydantic rejects empty / missing fields with 422
# ---------------------------------------------------------------------------


def test_resolve_rejects_empty_surface(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            resp = client.post(
                "/v1/identity/resolve",
                json={"surface": "", "surface_id": "x"},
            )

    assert resp.status_code == 422


def test_resolve_rejects_empty_surface_id(tmp_path: Path) -> None:
    """An empty ``surface_id`` would otherwise bind every empty-id
    caller to the same row. Reject at the validation layer."""
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            resp = client.post(
                "/v1/identity/resolve",
                json={"surface": "cli", "surface_id": ""},
            )

    assert resp.status_code == 422


def test_resolve_rejects_missing_surface_id(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    with respx.mock(base_url=settings.opencode_url, assert_all_called=False) as mock:
        _mock_happy_boot(mock)
        with TestClient(app) as client:
            resp = client.post(
                "/v1/identity/resolve",
                json={"surface": "cli"},
            )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Mounting smoke check
# ---------------------------------------------------------------------------


def test_identity_route_is_mounted(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    app = create_app(settings)
    paths = {r.path for r in app.routes}  # type: ignore[attr-defined]
    assert "/v1/identity/resolve" in paths

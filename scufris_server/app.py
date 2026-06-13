"""FastAPI application factory and lifespan (step 5 of #9).

The factory accepts an optional :class:`Settings` override so tests can
isolate state without touching environment variables. Production goes
through :func:`create_app` with no arguments (uvicorn's ``factory=True``
calls it without args), at which point :func:`get_settings` supplies
the cached process-wide config.

Lifespan order
--------------
1. Apply pending SQLite migrations (fatal on failure — wrong schema
   means no later step works).
2. Seed the ``users(id=1)`` placeholder row used by v0's hard-coded
   identity (real identity resolution lives in #12). Idempotent.
3. Construct the :class:`OpencodeClient` and stash it on
   ``app.state.opencode`` for dependency injection.
4. Probe ``/global/health`` with a 30s budget. *Failure is not fatal*;
   the server boots in degraded mode and step 7's ``/v1/healthz`` will
   report it.
5. If the health probe succeeded, probe ``/provider`` and cache a
   default ``ModelRef``. Failure here is also non-fatal; ``/v1/chat``
   hard-fails with 503 in that case until opencode comes back.
6. Yield to the running app.
7. On shutdown, close the opencode client.

State attached to ``app.state``
-------------------------------
- ``settings``: the resolved :class:`Settings` instance.
- ``opencode``: the live :class:`OpencodeClient`.
- ``opencode_initial_health``: :class:`HealthResponse` if the startup
  probe succeeded, else ``None`` (degraded boot marker).
- ``opencode_version``: cached opencode version string, populated by
  the startup probe and refreshed by ``/v1/healthz`` /
  ``/v1/version``. ``None`` until the first successful probe.
- ``opencode_default_model``: cached :class:`ModelRef` to use for
  ``/v1/chat`` when no explicit model is supplied. ``None`` when
  opencode has nothing connected — chat hard-fails with 503 in that
  state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from scufris_server import __version__
from scufris_server.config import Settings, get_settings
from scufris_server.logging import RequestIdMiddleware
from scufris_server.opencode_client import (
    HealthResponse,
    ModelRef,
    OpencodeClient,
    OpencodeError,
    OpencodeUnavailable,
)
from scufris_server.routes import ROUTERS
from scufris_server.store import apply_migrations, connect

logger = logging.getLogger("scufris_server")

# Budget for the startup health probe. Independent of the client's own
# per-request timeout so we can boot fast even if opencode hangs.
HEALTH_PROBE_TIMEOUT_S = 30.0

# Budget for the startup default-model probe. Same shape as the health
# probe — bounds boot time when opencode is reachable but slow.
DEFAULT_MODEL_PROBE_TIMEOUT_S = 30.0

# Hard-coded for v0 per #9 step 8; real identity resolution is #12.
DEFAULT_USER_ID = 1
DEFAULT_USERNAME = "default"


def _seed_default_user(settings: Settings) -> None:
    """Ensure ``users(id=1)`` exists so channels can FK to it.

    Idempotent: ``INSERT OR IGNORE`` is a no-op on subsequent boots.
    Uses a fresh connection from :func:`store.connect` rather than
    threading one through the lifespan — the cost is negligible (one
    statement) and it keeps the seed concern isolated.
    """
    with connect(settings) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, created_at) VALUES (?, ?, ?)",
            (DEFAULT_USER_ID, DEFAULT_USERNAME, int(time.time())),
        )
        conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage process-scoped resources for the duration of the app."""
    settings: Settings = app.state.settings

    # 1. Migrations — fatal if they fail.
    with connect(settings) as conn:
        applied = apply_migrations(conn)
    if applied:
        logger.info("applied migrations: %s", applied)
    else:
        logger.info("schema up to date (no pending migrations)")

    # 2. Seed the placeholder user row (idempotent).
    _seed_default_user(settings)

    # 3. opencode client.
    client = OpencodeClient(
        base_url=settings.opencode_url,
        password=settings.opencode_password,
    )
    app.state.opencode = client

    # 4. Startup health probe — non-fatal.
    health: HealthResponse | None = None
    try:
        health = await asyncio.wait_for(client.health(), timeout=HEALTH_PROBE_TIMEOUT_S)
        app.state.opencode_initial_health = health
        app.state.opencode_version = health.version
        logger.info(
            "opencode reachable: version=%s url=%s",
            health.version,
            settings.opencode_url,
        )
    except (OpencodeUnavailable, TimeoutError) as exc:
        app.state.opencode_initial_health = None
        app.state.opencode_version = None
        logger.warning(
            "opencode unreachable at %s (degraded boot): %s",
            settings.opencode_url,
            exc,
        )

    # 5. Default-model probe — only when opencode is up. Skip when
    # health failed; we'd just hit the same wall again. Cache stays
    # None, so /v1/chat will 503 until opencode comes back.
    default_model: ModelRef | None = None
    if health is not None:
        try:
            default_model = await asyncio.wait_for(
                client.get_default_model(),
                timeout=DEFAULT_MODEL_PROBE_TIMEOUT_S,
            )
        except (OpencodeError, TimeoutError) as exc:
            logger.warning(
                "default-model probe failed (chat will 503 until fixed): %s",
                exc,
                extra={"error": str(exc), "error_type": type(exc).__name__},
            )
        else:
            if default_model is None:
                logger.warning(
                    "opencode has no connected provider with a default model; "
                    "chat will 503 until one is configured",
                )
            else:
                logger.info(
                    "default model: %s/%s",
                    default_model.providerID,
                    default_model.modelID,
                    extra={
                        "default_provider": default_model.providerID,
                        "default_model": default_model.modelID,
                    },
                )
    app.state.opencode_default_model = default_model

    try:
        yield
    finally:
        # Shutdown.
        await client.close()
        logger.info("opencode client closed")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the FastAPI app.

    Parameters
    ----------
    settings
        Optional override. Production passes nothing — :func:`get_settings`
        supplies the cached env-driven instance. Tests should pass an
        explicit :class:`Settings` to isolate ``state_dir`` and
        ``opencode_url`` from the host.
    """
    app = FastAPI(
        title="scufris-server",
        version=__version__,
        description="Scufris daemon fronting opencode serve.",
        lifespan=lifespan,
    )
    app.state.settings = settings or get_settings()

    app.add_middleware(RequestIdMiddleware)

    for router in ROUTERS:
        app.include_router(router)

    return app

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
2. Construct the :class:`OpencodeClient` and stash it on
   ``app.state.opencode`` for dependency injection.
3. Probe ``/global/health`` with a 30s budget. *Failure is not fatal*;
   the server boots in degraded mode and step 7's ``/v1/healthz`` will
   report it.
4. Yield to the running app.
5. On shutdown, close the opencode client.

State attached to ``app.state``
-------------------------------
- ``settings``: the resolved :class:`Settings` instance.
- ``opencode``: the live :class:`OpencodeClient`.
- ``opencode_initial_health``: :class:`HealthResponse` if the startup
  probe succeeded, else ``None`` (degraded boot marker).
- ``opencode_version``: cached opencode version string, populated by
  the startup probe and refreshed by ``/v1/healthz`` /
  ``/v1/version``. ``None`` until the first successful probe.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from scufris_server import __version__
from scufris_server.config import Settings, get_settings
from scufris_server.logging import RequestIdMiddleware
from scufris_server.opencode_client import (
    HealthResponse,
    OpencodeClient,
    OpencodeUnavailable,
)
from scufris_server.routes import ROUTERS
from scufris_server.store import apply_migrations, connect

logger = logging.getLogger("scufris_server")

# Budget for the startup health probe. Independent of the client's own
# per-request timeout so we can boot fast even if opencode hangs.
HEALTH_PROBE_TIMEOUT_S = 30.0


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

    # 2. opencode client.
    client = OpencodeClient(
        base_url=settings.opencode_url,
        password=settings.opencode_password,
    )
    app.state.opencode = client

    # 3. Startup health probe — non-fatal.
    try:
        health: HealthResponse = await asyncio.wait_for(
            client.health(), timeout=HEALTH_PROBE_TIMEOUT_S
        )
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

    try:
        yield
    finally:
        # 5. Shutdown.
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

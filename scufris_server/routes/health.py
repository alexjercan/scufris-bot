"""``/v1/healthz`` and ``/v1/version`` (step 7 of #9).

Both endpoints are tolerant of opencode being down — they always
return 200 and report opencode status as data, so a load balancer
can keep scufris in rotation when the upstream blips.

``/v1/healthz``
   Re-probes ``opencode.health()`` on every request with a 5s budget.
   Body is ``{ok: bool, opencode: {healthy, version} | {error}}``.
   ``ok`` reflects whether opencode is healthy *right now* (not
   scufris's own liveness — if you got a 200, scufris is alive).

``/v1/version``
   Returns ``{version, opencode_version}``. ``version`` is scufris's
   own. ``opencode_version`` is read from a cache populated at boot
   and refreshed by ``/v1/healthz``; on cache miss the endpoint does
   a live probe (5s budget). On probe failure ``opencode_version``
   is ``null`` and the cache is left empty so the next call retries.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from scufris_server import __version__
from scufris_server.dependencies import get_opencode_client
from scufris_server.opencode_client import OpencodeClient, OpencodeUnavailable

# Probes have to be fast — these are health endpoints. The client's own
# 30s default would block load balancers and dashboards for far too long.
HEALTHZ_TIMEOUT_S = 5.0
VERSION_PROBE_TIMEOUT_S = 5.0

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["health"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class OpencodeOk(BaseModel):
    """opencode reachable — current health snapshot."""

    healthy: bool
    version: str


class OpencodeDown(BaseModel):
    """opencode unreachable — failure description."""

    error: str


class HealthzResponse(BaseModel):
    ok: bool
    opencode: OpencodeOk | OpencodeDown


class VersionResponse(BaseModel):
    version: str
    opencode_version: str | None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/healthz", response_model=HealthzResponse)
async def healthz(
    request: Request,
    client: Annotated[OpencodeClient, Depends(get_opencode_client)],
) -> HealthzResponse:
    """Probe opencode and report; always 200."""
    try:
        health = await asyncio.wait_for(client.health(), timeout=HEALTHZ_TIMEOUT_S)
    except (OpencodeUnavailable, TimeoutError) as exc:
        message = str(exc) or type(exc).__name__
        logger.warning(
            "healthz degraded: %s",
            message,
            extra={"error": message, "error_type": type(exc).__name__},
        )
        return HealthzResponse(ok=False, opencode=OpencodeDown(error=message))

    # Opportunistically refresh the version cache for /v1/version.
    request.app.state.opencode_version = health.version
    logger.info(
        "healthz ok: opencode_version=%s",
        health.version,
        extra={"opencode_version": health.version, "opencode_healthy": health.healthy},
    )
    return HealthzResponse(
        ok=True,
        opencode=OpencodeOk(healthy=health.healthy, version=health.version),
    )


@router.get("/version", response_model=VersionResponse)
async def version(
    request: Request,
    client: Annotated[OpencodeClient, Depends(get_opencode_client)],
) -> VersionResponse:
    """Return scufris's version + the cached opencode version.

    Probes opencode lazily on cache miss. Cache is filled by the
    startup health probe and by every successful ``/v1/healthz``,
    so steady-state calls don't hit opencode at all.
    """
    cached: str | None = getattr(request.app.state, "opencode_version", None)
    if cached is not None:
        logger.info(
            "version cache hit: opencode_version=%s",
            cached,
            extra={"opencode_version": cached, "cached": True},
        )
        return VersionResponse(version=__version__, opencode_version=cached)

    # Cache miss — probe and try to fill.
    try:
        health = await asyncio.wait_for(
            client.health(), timeout=VERSION_PROBE_TIMEOUT_S
        )
    except (OpencodeUnavailable, TimeoutError) as exc:
        message = str(exc) or type(exc).__name__
        logger.warning(
            "version probe failed: %s",
            message,
            extra={"error": message, "error_type": type(exc).__name__},
        )
        return VersionResponse(version=__version__, opencode_version=None)

    request.app.state.opencode_version = health.version
    logger.info(
        "version cache populated: opencode_version=%s",
        health.version,
        extra={"opencode_version": health.version, "cached": False},
    )
    return VersionResponse(version=__version__, opencode_version=health.version)

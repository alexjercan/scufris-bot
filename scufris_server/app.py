"""FastAPI application factory and lifespan for the Scufris HTTP server."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from fastapi import FastAPI

from .bootstrap import Runtime, build_runtime
from .routes import admin, chat, stats

logger = logging.getLogger("scufris-server")


def _shutdown_grace_seconds() -> float:
    raw = os.environ.get("SCUFRIS_SHUTDOWN_GRACE", "30")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 30.0


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime = build_runtime()
    app.state.runtime = runtime
    app.state.started_at = datetime.now(timezone.utc)
    logger.info("scufris-server startup complete")
    try:
        yield
    finally:
        # Graceful shutdown: wait up to SCUFRIS_SHUTDOWN_GRACE seconds
        # for in-flight requests to drain. We don't track every request
        # explicitly — relying on uvicorn's own task accounting via
        # the asyncio task list is good enough at this scale.
        grace = _shutdown_grace_seconds()
        if grace > 0:
            current = asyncio.current_task()
            pending = [
                t for t in asyncio.all_tasks() if t is not current and not t.done()
            ]
            if pending:
                logger.info(
                    "draining %d in-flight tasks (up to %.1fs)…",
                    len(pending),
                    grace,
                )
                done, still = await asyncio.wait(pending, timeout=grace)
                if still:
                    logger.warning(
                        "shutdown: %d task(s) did not finish in time",
                        len(still),
                    )
        logger.info("scufris-server shutdown complete")


def create_app(runtime: Optional[Runtime] = None) -> FastAPI:
    """Construct a FastAPI app.

    When ``runtime`` is provided the lifespan is skipped — used by the
    test suite to inject a stub agent without bootstrapping the real
    one. In production the lifespan calls :func:`build_runtime`.
    """
    if runtime is not None:
        app = FastAPI(title="scufris-server")
        app.state.runtime = runtime
        app.state.started_at = datetime.now(timezone.utc)
    else:
        app = FastAPI(title="scufris-server", lifespan=_lifespan)

    app.include_router(admin.router)
    app.include_router(chat.router)
    app.include_router(stats.router)
    return app

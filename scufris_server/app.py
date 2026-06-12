"""FastAPI application factory and lifespan for the Scufris HTTP server."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from fastapi import FastAPI

from .bootstrap import Runtime, build_runtime
from .routes import admin, chat, identity, opencode, stats

logger = logging.getLogger("scufris-server")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime = build_runtime()
    app.state.runtime = runtime
    app.state.started_at = datetime.now(timezone.utc)
    # Start the shared OpenCode /event listener so concurrent chat
    # turns share a single upstream connection (task 105013). On
    # failure (OpenCode unreachable, transient network) the bus
    # itself reconnects with backoff — startup must not block on it.
    try:
        await runtime.opencode_client.start_event_bus()
        logger.info("opencode event bus started")
    except Exception:  # noqa: BLE001 — bus errors recover via reconnect
        logger.exception("start_event_bus raised; continuing without bus")
    # Best-effort: drop any persisted user_id -> session_id entries
    # whose upstream session no longer exists. Failures here are
    # already logged by the AgentManager and never abort startup.
    try:
        pruned = await runtime.agent_manager.prune_invalid_sessions()
        if pruned:
            logger.info("startup: pruned %d stale OpenCode session(s)", pruned)
    except Exception:  # noqa: BLE001 — startup must never fail on pruning
        logger.exception("prune_invalid_sessions raised; continuing startup")
    logger.info("scufris-server startup complete")
    try:
        yield
    finally:
        # Graceful shutdown: wait up to server.shutdown_grace seconds
        # for in-flight requests to drain. We don't track every request
        # explicitly — relying on uvicorn's own task accounting via
        # the asyncio task list is good enough at this scale.
        grace = max(0.0, float(runtime.config.server.shutdown_grace))
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
        # Close the OpenCode HTTP client (releases the connection pool
        # and the long-lived /event stream if any chat_stream is still
        # alive after task cancellation above).
        try:
            await runtime.opencode_client.aclose()
        except Exception:  # noqa: BLE001 — never hide other shutdown errors
            logger.exception("opencode client aclose() raised")
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
    app.include_router(identity.router)
    app.include_router(opencode.router)
    app.include_router(stats.router)
    return app

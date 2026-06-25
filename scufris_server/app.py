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
2. Seed the ``users(id=1)`` placeholder row used as the default
   identity. ``resolve_user`` falls back here when no TOML user
   matches and no override is set. Idempotent.
3. Load ``config.toml`` via :func:`scufris_server.identity.load_user_identity`
   and cache on ``app.state.user_identity``. Always populated —
   missing file produces an empty :class:`IdentityFile`.
4. Validate ``SCUFRIS_USER_ID`` if set: the override id must exist
   in ``users``. Cache the validated override on
   ``app.state.identity_override``. Fail fast (RuntimeError) on a
   stale value — discovering it on the first chat request is worse.
5. Construct the :class:`OpencodeClient` and stash it on
   ``app.state.opencode`` for dependency injection.
6. Probe ``/global/health`` with a 30s budget. *Failure is not fatal*;
   the server boots in degraded mode and step 7's ``/v1/healthz`` will
   report it.
7. If the health probe succeeded, probe ``/provider`` and cache a
   default ``ModelRef``. Failure here is also non-fatal; ``/v1/chat``
   hard-fails with 503 in that case until opencode comes back.
8. Construct the :class:`EventBus` borrowing the client's
   :class:`httpx.AsyncClient` (#11 step 7) and call :meth:`start`.
   The bus's reader task tolerates upstream failures itself
   (reconnect loop with exponential backoff per ADR-10), so
   :meth:`start` is non-blocking and always succeeds — degraded
   boots get a bus that keeps retrying in the background until
   opencode comes back. The streaming chat handler
   (``POST /v1/chat/stream``, step 8 of #11) raises
   :class:`OpencodeUnavailable` if the bus hasn't connected by the
   ``subscribe()`` timeout, which is the user-visible failure
   surface for upstream death.
9. Yield to the running app.
10. On shutdown, stop the event bus first (so it can't try to use
    the httpx transport mid-close), then close the opencode client.

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
- ``opencode_event_bus`` (#11): the live :class:`EventBus`. Started
  by the lifespan, consumed by ``POST /v1/chat/stream``. Always
  attached, even on degraded boots.
- ``user_identity`` (#12): the parsed :class:`IdentityFile` from
  ``config.toml``. Always present; ``user=None`` when no file
  exists.
- ``identity_override`` (#12): :data:`Settings.user_id` after
  validation against the ``users`` table, or ``None``. When
  non-None, every ``resolve_user`` call short-circuits to this id.
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
from scufris_server.events import EventBus
from scufris_server.identity import (
    DEFAULT_USER_ID,
    DEFAULT_USERNAME,
    IdentityFile,
    load_user_identity,
)
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


def _validate_identity_override(settings: Settings) -> int | None:
    """Validate ``settings.user_id`` against the users table.

    Returns the validated override (or ``None`` when unset). Raises
    :class:`RuntimeError` if the override points at a non-existent
    user — that's a misconfiguration we want surfaced at boot, not on
    the first chat request.
    """
    if settings.user_id is None:
        return None
    with connect(settings) as conn:
        row = conn.execute(
            "SELECT username FROM users WHERE id = ?", (settings.user_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError(
            f"SCUFRIS_USER_ID={settings.user_id} is set but no user with "
            "that id exists. Either remove the env var, lower it to 1 "
            "(default user), or pre-seed the users table via config.toml."
        )
    logger.info(
        "identity override active: user_id=%d (%s)",
        settings.user_id,
        row["username"],
        extra={
            "override_user_id": settings.user_id,
            "override_username": row["username"],
        },
    )
    return settings.user_id


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

    # 3. Load the user-identity TOML. Always present on app.state;
    # missing config.toml → empty IdentityFile (default user only).
    identity_file: IdentityFile = load_user_identity(settings.config_path)
    app.state.user_identity = identity_file

    # 4. Validate SCUFRIS_USER_ID override. Fails fast on misconfig.
    app.state.identity_override = _validate_identity_override(settings)

    # 5. opencode client.
    client = OpencodeClient(
        base_url=settings.opencode_url,
        password=settings.opencode_password,
    )
    app.state.opencode = client

    # 6. Startup health probe — non-fatal.
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

    # 7. Default-model probe — only when opencode is up. Skip when
    # health failed; we'd just hit the same wall again. Cache stays
    # None, so /v1/chat will 503 until opencode comes back.
    # If ``settings.opencode_model`` is set (via OPENCODE_MODEL env
    # var) skip the API probe and construct a ModelRef directly.
    default_model: ModelRef | None = None
    if settings.opencode_model is not None:
        if "/" in settings.opencode_model:
            provider_id, model_id = settings.opencode_model.split("/", 1)
        else:
            provider_id = "ollama"
            model_id = settings.opencode_model
        default_model = ModelRef(providerID=provider_id, modelID=model_id)
        logger.info(
            "default model overridden via OPENCODE_MODEL: %s/%s",
            provider_id,
            model_id,
            extra={
                "default_provider": provider_id,
                "default_model": model_id,
            },
        )
    elif health is not None:
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

    # 8. Event bus — single shared GET /event consumer per process
    # (ADR-10; #11 step 7). Borrows the client's httpx transport via
    # its ``httpx_client`` property. ``start()`` only spawns the
    # background reader task and returns immediately; the task's
    # own reconnect loop handles upstream death, so this is safe
    # to call on a degraded boot too — the bus just keeps retrying
    # until opencode comes back.
    bus = EventBus(client.httpx_client)
    await bus.start()
    app.state.opencode_event_bus = bus

    try:
        yield
    finally:
        # Shutdown. Stop the bus FIRST so its reader task isn't
        # mid-stream when ``client.close()`` tears down the
        # transport (which would surface as a noisy CancelledError
        # in the reconnect loop's logs).
        await bus.stop()
        logger.info("opencode event bus stopped")
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

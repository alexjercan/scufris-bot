"""FastAPI dependencies for route handlers.

Lives separately from :mod:`scufris_server.app` to break the import
cycle: routes need DI helpers, ``app`` mounts the routers, so the
helpers must live in a module that neither side has to import the
other to reach.

Step 7 added :func:`get_opencode_client`. Step 8 (chat) adds
:func:`get_db_conn` so handlers can use SQLite without re-deriving
the path or the lifespan singleton. #12 (identity) adds
:func:`get_user_identity` and :func:`get_identity_override` so chat
and identity routes can read the lifespan-cached config.toml +
``SCUFRIS_USER_ID`` override. #11 step 7 adds :func:`get_event_bus`
so the streaming chat handler (``POST /v1/chat/stream``, step 8 of
#11) can subscribe to the process-wide event bus.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator

from fastapi import Request

from scufris_server.events import EventBus
from scufris_server.identity import IdentityFile
from scufris_server.opencode_client import OpencodeClient
from scufris_server.store import connect


def get_opencode_client(request: Request) -> OpencodeClient:
    """FastAPI dependency: return the lifespan-scoped opencode client.

    Usage::

        from fastapi import Depends
        from scufris_server.dependencies import get_opencode_client

        @router.get("/foo")
        async def foo(oc: OpencodeClient = Depends(get_opencode_client)):
            ...
    """
    client: OpencodeClient = request.app.state.opencode
    return client


async def get_db_conn(request: Request) -> AsyncIterator[sqlite3.Connection]:
    """FastAPI dependency: yield a per-request SQLite connection.

    Honours the request-scoped :class:`scufris_server.config.Settings`
    that :func:`scufris_server.app.create_app` stashed at startup, so
    test apps with overridden ``state_dir`` get isolated DB files.

    Async vs sync: this is declared ``async`` deliberately. SQLite
    connections are pinned to the thread that opened them
    (``check_same_thread=True``), and FastAPI runs sync generator
    deps in its threadpool while async handlers stay on the event
    loop — a sync dep would yield a connection that the async
    handler can't legally touch. Going async keeps the dep on the
    same loop thread as the handler. The cost is minimal: opening a
    WAL-mode SQLite connection is a microsecond-scale operation, so
    blocking the event loop briefly is fine.

    Transaction policy: handlers manage their own commits/rollbacks
    via ``with conn:`` blocks. We don't auto-commit here because read
    paths shouldn't issue silent BEGINs and write paths should be
    explicit about the unit of work.

    On request exit (success or failure) the underlying connection is
    closed by :func:`scufris_server.store.connect`. Any uncommitted
    transaction is rolled back by SQLite at close time, so a crashed
    handler can't leak partial state.

    Usage::

        from fastapi import Depends
        from scufris_server.dependencies import get_db_conn

        @router.post("/foo")
        async def foo(conn: sqlite3.Connection = Depends(get_db_conn)):
            ...
    """
    settings = request.app.state.settings
    with connect(settings) as conn:
        yield conn


def get_user_identity(request: Request) -> IdentityFile:
    """FastAPI dependency: return the lifespan-loaded identity config.

    The :class:`IdentityFile` is parsed once at startup (see
    :func:`scufris_server.app.lifespan`) and stashed on
    ``app.state.user_identity``. Always present — when no
    ``config.toml`` is found, the lifespan stores an empty
    :class:`IdentityFile` (``user=None``), which makes every
    :func:`scufris_server.identity.resolve_user` call fall through
    to the default user.

    No reload-on-change: the v1 design (and ours) requires a
    restart to pick up TOML edits. Hot-reload via SIGHUP is
    explicitly out of scope for #12.
    """
    identity: IdentityFile = request.app.state.user_identity
    return identity


def get_identity_override(request: Request) -> int | None:
    """FastAPI dependency: return ``SCUFRIS_USER_ID`` if set, else None.

    The override (#12 D4) pins every ``resolve_user`` call to a
    single ``user_id`` for the process's lifetime. It's set once at
    startup from :class:`Settings.user_id` and stashed on
    ``app.state.identity_override``.

    The lifespan validates that the override id exists in the
    ``users`` table before yielding control — so a non-None value
    here is guaranteed to resolve cleanly.
    """
    override: int | None = request.app.state.identity_override
    return override


def get_event_bus(request: Request) -> EventBus:
    """FastAPI dependency: return the lifespan-scoped opencode event bus.

    The bus is the single process-wide consumer of opencode's
    ``GET /event`` SSE stream (ADR-10). It's constructed and
    started by :func:`scufris_server.app.lifespan` (#11 step 7)
    and stashed on ``app.state.opencode_event_bus``. Always
    present, even on degraded boots — the bus's own reconnect
    loop handles upstream unavailability.

    Consumed by the streaming chat handler
    (``POST /v1/chat/stream``, #11 step 8) which calls
    ``bus.subscribe(session_id)`` for the duration of a turn.

    Usage::

        from fastapi import Depends
        from scufris_server.dependencies import get_event_bus

        @router.post("/foo")
        async def foo(bus: EventBus = Depends(get_event_bus)):
            async with bus.subscribe(session_id) as queue:
                ...
    """
    bus: EventBus = request.app.state.opencode_event_bus
    return bus

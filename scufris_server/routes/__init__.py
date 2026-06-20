"""Route registration entry point.

Routers live in sibling modules (``health.py`` — step 7, ``chat.py`` —
step 8, etc.). Each module exposes a ``router: APIRouter`` and is
appended to :data:`ROUTERS` here. The app factory
(:func:`scufris_server.app.create_app`) iterates :data:`ROUTERS` and
calls ``include_router`` on each.

Step 7 added healthz/version; step 8 added chat. #12 promoted
identity from placeholder to real (``POST /v1/identity/resolve``).
#10 promoted sessions, mounting two routers:

- ``sessions_router`` (prefix ``/v1/sessions``) — list +
  per-channel clear.
- ``clear_router`` (prefix ``/v1``) — bulk clear
  (``POST /v1/clear``).

The two-router split is a deliberate choice (D4) — see the
:mod:`scufris_server.routes.sessions` module docstring.

#11 step 9 (``20260613-091045``) mounted
:mod:`scufris_server.routes.chat_stream` (``POST /v1/chat/stream``),
the SSE streaming counterpart to ``/v1/chat``. The module shares
helpers with :mod:`scufris_server.routes.chat` via cross-route
import (#11 D6) — kept as a separate router rather than another
endpoint on ``chat_router`` so the module stays focused on the
streaming pipeline (bus subscribe → background poster → event
mapping → SSE framing).

Placeholder routers (defined but not yet mounted; future tasks
register real routes and append to :data:`ROUTERS` themselves):

- :mod:`scufris_server.routes.stats`       — task #13
  (``tasks/20260613-091047``).
- :mod:`scufris_server.routes.permissions` — task #30
  (``tasks/20260613-093108``).

We import the placeholders here so they're discoverable via
``import scufris_server.routes`` and so any import-time error in a
placeholder shows up immediately rather than waiting for the dependent
task to land.
"""

from __future__ import annotations

from fastapi import APIRouter

from scufris_server.routes import permissions, stats  # noqa: F401
from scufris_server.routes.chat import router as chat_router
from scufris_server.routes.chat_stream import router as chat_stream_router
from scufris_server.routes.health import router as health_router
from scufris_server.routes.identity import router as identity_router
from scufris_server.routes.sessions import clear_router, sessions_router

ROUTERS: list[APIRouter] = [
    health_router,
    identity_router,
    chat_router,
    chat_stream_router,
    sessions_router,
    clear_router,
]

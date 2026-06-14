"""Route registration entry point.

Routers live in sibling modules (``health.py`` — step 7, ``chat.py`` —
step 8, etc.). Each module exposes a ``router: APIRouter`` and is
appended to :data:`ROUTERS` here. The app factory
(:func:`scufris_server.app.create_app`) iterates :data:`ROUTERS` and
calls ``include_router`` on each.

Step 7 added healthz/version; step 8 added chat; step 10 lands
placeholder modules for the rest.

Placeholder routers (defined but not yet mounted; future tasks register
real routes and append to :data:`ROUTERS` themselves):

- :mod:`scufris_server.routes.identity`    — task #12
  (``tasks/20260613-091046``).
- :mod:`scufris_server.routes.sessions`    — task #10
  (``tasks/20260613-091044``).
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

from scufris_server.routes import identity, permissions, sessions, stats  # noqa: F401
from scufris_server.routes.chat import router as chat_router
from scufris_server.routes.health import router as health_router

ROUTERS: list[APIRouter] = [health_router, chat_router]

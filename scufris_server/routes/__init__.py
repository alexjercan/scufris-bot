"""Route registration entry point.

Routers live in sibling modules (``health.py`` — step 7, ``chat.py`` —
step 8, etc.). Each module exposes a ``router: APIRouter`` and is
appended to :data:`ROUTERS` here. The app factory
(:func:`scufris_server.app.create_app`) iterates :data:`ROUTERS` and
calls ``include_router`` on each.

Step 7 added healthz/version; step 8 will add chat; step 10 lands
placeholder modules for the rest.
"""

from __future__ import annotations

from fastapi import APIRouter

from scufris_server.routes.health import router as health_router

ROUTERS: list[APIRouter] = [health_router]

"""Route registration entry point.

Routers live in sibling modules (``healthz.py`` — step 7, ``chat.py`` —
step 8, etc.). Each module exposes a ``router: APIRouter`` and is
appended to :data:`ROUTERS` here. The app factory
(:func:`scufris_server.app.create_app`) iterates :data:`ROUTERS` and
calls ``include_router`` on each.

Empty in step 5 by design — step 7 fills in healthz/version, step 8
adds chat, step 10 lands placeholder modules for the rest.
"""

from __future__ import annotations

from fastapi import APIRouter

ROUTERS: list[APIRouter] = []

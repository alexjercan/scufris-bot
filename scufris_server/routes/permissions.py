"""Placeholder router for opencode permission UX bridge (task #30 —
``tasks/20260613-093108``: "Permissions UX bridge: opencode
permission.asked → Telegram inline buttons").

This module exists so dependent tasks can import
``scufris_server.routes.permissions`` without ``ImportError`` and so
the file structure laid out in #9's design is in place.

The router is empty. Task #30 will populate it with at least
``POST /v1/permissions/{perm_id}/reply`` — the surface the user-facing
clients (Telegram, CLI) use to forward an "allow / allow once / deny"
choice back to opencode. Until then, any request under this prefix
will 404. We deliberately don't register a catch-all 501, since that
would shadow the real routes #30 adds.

When #30 lands it should:

1. Add concrete routes to the ``router`` defined here.
2. Append ``router`` to ``ROUTERS`` in :mod:`scufris_server.routes`.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/v1/permissions", tags=["permissions"])

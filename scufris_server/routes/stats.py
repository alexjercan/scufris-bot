"""Placeholder router for stats / clear endpoints (task #13 —
``tasks/20260613-091047``: "/stats and /clear endpoints + per-user
telemetry").

This module exists so dependent tasks can import
``scufris_server.routes.stats`` without ``ImportError`` and so the
file structure laid out in #9's design is in place.

The router is empty. Task #13 will populate it with ``/v1/stats`` and
``/v1/clear`` (plus their per-user telemetry surface). Until then, any
request under this prefix will 404. We deliberately don't register a
catch-all 501, since that would shadow the real routes #13 adds.

When #13 lands it should:

1. Add concrete routes to the ``router`` defined here.
2. Append ``router`` to ``ROUTERS`` in :mod:`scufris_server.routes`.
"""

from __future__ import annotations

from fastapi import APIRouter

# /v1/clear is also part of #13 but lives logically with stats; it can
# either share this router or grow its own ``routes/clear.py``. The
# task author can decide.
router = APIRouter(prefix="/v1/stats", tags=["stats"])

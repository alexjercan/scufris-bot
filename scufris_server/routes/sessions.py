"""Placeholder router for per-user opencode session management
(task #10 — ``tasks/20260613-091044``: "Per-user opencode session
management (create/resume/expire)").

This module exists so dependent tasks can import
``scufris_server.routes.sessions`` without ``ImportError`` and so the
file structure laid out in #9's design is in place.

The router is empty. Task #10 will populate it with the session-
management surface (list / fork / clear) at ``/v1/sessions``. Until
then, any request under this prefix will 404. We deliberately don't
register a catch-all 501, since that would shadow the real routes
#10 adds.

When #10 lands it should:

1. Add concrete routes to the ``router`` defined here.
2. Append ``router`` to ``ROUTERS`` in :mod:`scufris_server.routes`.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

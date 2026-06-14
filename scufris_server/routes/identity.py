"""Placeholder router for the identity HTTP layer (task #12 —
``tasks/20260613-091046``: "Identity layer + XDG user config").

This module exists so dependent tasks can import
``scufris_server.routes.identity`` without ``ImportError`` and so the
file structure laid out in #9's design is in place.

The router is empty (no routes registered). Task #12 will populate it
with at least ``POST /v1/identity/resolve``. Until then, any request
under this prefix will 404 — Starlette's default behaviour for an
unmatched path. We deliberately don't register a catch-all 501, since
that would shadow the real routes #12 adds.

When #12 lands it should:

1. Add concrete routes to the ``router`` defined here.
2. Append ``router`` to ``ROUTERS`` in :mod:`scufris_server.routes`.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/v1/identity", tags=["identity"])

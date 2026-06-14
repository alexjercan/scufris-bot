"""HTTP layer for the identity resolution module (#12).

Single endpoint: ``POST /v1/identity/resolve``. Body ``{surface,
surface_id}``; response :class:`scufris_server.identity.ResolvedUser`.

The route is an *almost*-direct passthrough to
:func:`scufris_server.identity.resolve_user`. All real logic — TOML
lookup, override handling, surface_bindings materialisation — lives
in the pure-Python ``identity`` module. This module just:

1. Validates the request body (surface + surface_id non-empty).
2. Plumbs in the request-scoped DB connection plus the lifespan-
   cached ``IdentityFile`` and override.
3. Logs the resolution at INFO with the request_id so operators
   can correlate against the chat call that triggered it.

The endpoint is unauthenticated for v0 — `/v1/*` is loopback-only
until #14 lands a bearer-token gate.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from scufris_server.dependencies import (
    get_db_conn,
    get_identity_override,
    get_user_identity,
)
from scufris_server.identity import IdentityFile, ResolvedUser, resolve_user

router = APIRouter(prefix="/v1/identity", tags=["identity"])
logger = logging.getLogger("scufris_server.routes.identity")


class ResolveRequest(BaseModel):
    """Body of ``POST /v1/identity/resolve``.

    Fields match design doc §9 line 291. Both are required, both
    non-empty — empty surface_id has no meaningful interpretation
    (would bind every empty-id caller to the same row), so reject
    at the validation layer.
    """

    surface: str = Field(
        ..., min_length=1, description="cli | telegram | web | ..."
    )
    surface_id: str = Field(
        ...,
        min_length=1,
        description="Per-surface user identifier (terminal user, "
        "telegram chat_id, web tab id, ...).",
    )


@router.post("/resolve", response_model=ResolvedUser)
async def resolve(
    payload: ResolveRequest,
    conn: Annotated[sqlite3.Connection, Depends(get_db_conn)],
    identity_file: Annotated[IdentityFile, Depends(get_user_identity)],
    override: Annotated[int | None, Depends(get_identity_override)],
) -> ResolvedUser:
    """Resolve ``(surface, surface_id)`` to a :class:`ResolvedUser`.

    Materialises ``users`` + ``surface_bindings`` rows on first
    contact (TOML hit or default fallback). See
    :func:`scufris_server.identity.resolve_user` for the algorithm.

    Errors
    ------
    - 422 (FastAPI default): empty surface or surface_id.
    - 500: ``RuntimeError`` from the identity module bubbles up;
      indicates a mis-seeded DB (default user missing) or a stale
      ``SCUFRIS_USER_ID`` env var pointing at a non-existent user.
      We don't catch these — they're operator-visible bugs.
    """
    resolved = resolve_user(
        conn,
        payload.surface,
        payload.surface_id,
        identity_file,
        override_user_id=override,
    )
    logger.info(
        "identity resolve: user_id=%d (%s) for (%s, %s)",
        resolved.user_id,
        resolved.username,
        resolved.surface,
        resolved.surface_id,
        extra={
            "user_id": resolved.user_id,
            "username": resolved.username,
            "surface": resolved.surface,
            "surface_id": resolved.surface_id,
            "override_active": override is not None,
        },
    )
    return resolved

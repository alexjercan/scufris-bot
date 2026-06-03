"""Identity resolution endpoint.

Maps a (surface, surface_id) pair — e.g. ``("cli", "alex")`` or
``("telegram", "8231376426")`` — to the canonical integer ``user_id``
that the rest of the server uses for history, telemetry, etc.

Resolution rules live in :func:`utils.user_config.resolve_user_id` so
the same logic is reachable from tests and (eventually) other backends.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from utils import resolve_user_id

from ..auth import require_token

router = APIRouter(prefix="/v1", tags=["identity"])
logger = logging.getLogger("scufris-server.identity")


class ResolveRequest(BaseModel):
    surface: str = Field(..., min_length=1)
    surface_id: str = Field(..., min_length=1)


class ResolveResponse(BaseModel):
    user_id: int
    username: Optional[str] = None
    surface: str
    surface_id: str
    # Surfaces explicitly bound in the config (e.g. ["cli", "telegram"]).
    # Empty when the resolution fell through to the hash/int fallback.
    bound_surfaces: List[str] = Field(default_factory=list)


@router.post(
    "/identity/resolve",
    dependencies=[Depends(require_token)],
    response_model=ResolveResponse,
)
async def resolve(request: Request, body: ResolveRequest) -> ResolveResponse:
    runtime = request.app.state.runtime
    config = runtime.config
    try:
        resolved = resolve_user_id(body.surface, body.surface_id, config)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "identity resolution failed for surface=%s id=%s",
            body.surface,
            body.surface_id,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    bound: List[str] = []
    if resolved.username and config.user.username == resolved.username:
        bound = sorted(config.user.identity.bindings.keys())

    return ResolveResponse(
        user_id=resolved.user_id,
        username=resolved.username,
        surface=body.surface,
        surface_id=body.surface_id,
        bound_surfaces=bound,
    )

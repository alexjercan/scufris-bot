"""Stats and history-management endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from utils.stats import format_stats_lines

from ..auth import require_token

router = APIRouter(prefix="/v1", tags=["stats"])


class ClearRequest(BaseModel):
    user_id: int


class ClearResponse(BaseModel):
    user_id: int
    cleared: int
    breakdown: Dict[str, int]


class StatsResponse(BaseModel):
    user_id: int
    lines: List[str]
    rows: Dict[str, Dict[str, Any]]


@router.get(
    "/stats",
    dependencies=[Depends(require_token)],
    response_model=StatsResponse,
)
async def stats(request: Request, user_id: int = Query(...)) -> StatsResponse:
    runtime = request.app.state.runtime
    started_at: datetime = request.app.state.started_at
    lines = format_stats_lines(
        runtime.history_manager,
        user_id,
        started_at=started_at,
        model=runtime.config.ollama.model,
        base_url=runtime.config.ollama.base_url,
    )
    rows = runtime.history_manager.get_user_telemetry(user_id)
    return StatsResponse(user_id=user_id, lines=lines, rows=rows)


@router.post(
    "/clear",
    dependencies=[Depends(require_token)],
    response_model=ClearResponse,
)
async def clear(request: Request, body: ClearRequest) -> ClearResponse:
    runtime = request.app.state.runtime
    breakdown = runtime.history_manager.get_user_breakdown(body.user_id)
    cleared = runtime.history_manager.clear_user(body.user_id)
    return ClearResponse(user_id=body.user_id, cleared=cleared, breakdown=breakdown)

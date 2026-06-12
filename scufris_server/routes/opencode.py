"""Proxy endpoints onto the local OpenCode daemon.

These are read/inspect routes for the CLI's status pane and future
clients — chat still goes through ``/v1/chat`` (which transparently
manages a session per user). All endpoints are auth-gated by the
same ``SCUFRIS_TOKEN`` bearer check as everywhere else.

The proxy bodies pass JSON through verbatim from OpenCode; we don't
re-validate via the SDK's typed models (they're partly stale — see
``tasks/20260610-101413/SCHEMA.md``).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request

from utils import OpenCodeError, OpenCodeStaleSessionError

from ..auth import require_token

router = APIRouter(prefix="/v1/opencode", tags=["opencode"])
logger = logging.getLogger("scufris-server.opencode")


def _client(request: Request):
    """Pull the shared OpenCodeClient off the runtime."""
    return request.app.state.runtime.opencode_client


@router.get("/sessions", dependencies=[Depends(require_token)])
async def list_sessions(request: Request) -> List[Dict[str, Any]]:
    try:
        return await _client(request).list_sessions()
    except OpenCodeError as exc:
        logger.warning("opencode list_sessions failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get(
    "/sessions/{session_id}",
    dependencies=[Depends(require_token)],
)
async def get_session(request: Request, session_id: str) -> Dict[str, Any]:
    try:
        return await _client(request).get_session(session_id)
    except OpenCodeStaleSessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OpenCodeError as exc:
        logger.warning("opencode get_session(%s) failed: %s", session_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete(
    "/sessions/{session_id}",
    dependencies=[Depends(require_token)],
)
async def delete_session(request: Request, session_id: str) -> Dict[str, str]:
    """Delete a session on OpenCode.

    Also forgets it from the per-user session map, so the next chat
    turn from any user mapped to this id creates a fresh session.
    Idempotent: a 404 from OpenCode (already gone) is treated as
    success.
    """
    runtime = request.app.state.runtime
    # Forget any user_id → session_id mapping pointing at this session.
    sessions_snapshot = runtime.agent_manager.sessions
    matching_users = [
        uid for uid, sid in sessions_snapshot.items() if sid == session_id
    ]
    for uid in matching_users:
        await runtime.agent_manager.delete_session(uid)
    if not matching_users:
        # Not in our map — proxy the delete anyway.
        try:
            await _client(request).delete_session(session_id)
        except OpenCodeError as exc:
            logger.warning("opencode delete_session(%s) failed: %s", session_id, exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"deleted": session_id}


@router.get("/models", dependencies=[Depends(require_token)])
async def list_models(request: Request) -> Dict[str, Any]:
    try:
        return await _client(request).list_models()
    except OpenCodeError as exc:
        logger.warning("opencode list_models failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/providers", dependencies=[Depends(require_token)])
async def list_providers(request: Request) -> Dict[str, Any]:
    try:
        return await _client(request).list_providers()
    except OpenCodeError as exc:
        logger.warning("opencode list_providers failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

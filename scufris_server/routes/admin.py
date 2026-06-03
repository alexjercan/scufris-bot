"""Health, readiness and version endpoints."""

from __future__ import annotations

import time
from typing import Any, Dict

import httpx
from fastapi import APIRouter, Depends, Request

from ..auth import require_token

router = APIRouter(prefix="/v1", tags=["admin"])

# Tiny TTL cache so /readyz under hammering doesn't DOS Ollama.
_READY_TTL_SECONDS: float = 5.0
_ready_cache: Dict[str, Any] = {"ts": 0.0, "result": None}


@router.get("/healthz")
async def healthz() -> Dict[str, str]:
    """Liveness probe — process is up. Doesn't touch external systems."""
    return {"status": "ok"}


@router.get("/readyz", dependencies=[Depends(require_token)])
async def readyz(request: Request) -> Dict[str, Any]:
    """Readiness probe — checks Ollama is reachable. Cached for 5s."""
    now = time.monotonic()
    cached = _ready_cache["result"]
    if cached is not None and now - _ready_cache["ts"] < _READY_TTL_SECONDS:
        return dict(cached)

    runtime = request.app.state.runtime
    base_url = runtime.config.ollama.base_url
    result: Dict[str, Any]
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
        result = {
            "status": "ready" if resp.status_code < 500 else "degraded",
            "ollama": {"base_url": base_url, "code": resp.status_code},
        }
    except Exception as exc:  # noqa: BLE001 — readiness reports any failure
        result = {
            "status": "degraded",
            "ollama": {"base_url": base_url, "error": str(exc)},
        }
    _ready_cache["ts"] = now
    _ready_cache["result"] = result
    return dict(result)


@router.get("/version", dependencies=[Depends(require_token)])
async def version(request: Request) -> Dict[str, Any]:
    """Build / config snapshot useful for the CLI's status pane."""
    runtime = request.app.state.runtime
    return {
        "name": "scufris-server",
        "version": "0.1.0",
        "model": runtime.config.ollama.model,
        "ollama_base_url": runtime.config.ollama.base_url,
    }

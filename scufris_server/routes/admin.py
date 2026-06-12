"""Health, readiness and version endpoints."""

from __future__ import annotations

import time
from typing import Any, Dict

import httpx
from fastapi import APIRouter, Depends, Request

from ..auth import require_token

router = APIRouter(prefix="/v1", tags=["admin"])

# Tiny TTL cache so /readyz under hammering doesn't DOS Ollama or
# OpenCode.
_READY_TTL_SECONDS: float = 5.0
_ready_cache: Dict[str, Any] = {"ts": 0.0, "result": None}


@router.get("/healthz")
async def healthz() -> Dict[str, str]:
    """Liveness probe — process is up. Doesn't touch external systems."""
    return {"status": "ok"}


@router.get("/readyz", dependencies=[Depends(require_token)])
async def readyz(request: Request) -> Dict[str, Any]:
    """Readiness probe — pings Ollama and OpenCode. Cached for 5s.

    The Ollama ping stays for as long as the compactor still uses
    Ollama; once ``tasks/20260610-105002`` rewrites the compactor onto
    OpenCode (or a slimmer Ollama HTTP path) this can drop.
    """
    now = time.monotonic()
    cached = _ready_cache["result"]
    if cached is not None and now - _ready_cache["ts"] < _READY_TTL_SECONDS:
        return dict(cached)

    runtime = request.app.state.runtime

    # Ollama ping
    ollama_url = runtime.config.ollama.base_url
    ollama_status: Dict[str, Any]
    ollama_ok: bool
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{ollama_url.rstrip('/')}/api/tags")
        ollama_ok = resp.status_code < 500
        ollama_status = {"base_url": ollama_url, "code": resp.status_code}
    except Exception as exc:  # noqa: BLE001 — readiness reports any failure
        ollama_ok = False
        ollama_status = {"base_url": ollama_url, "error": str(exc)}

    # OpenCode ping — `GET /session` returns the (possibly empty)
    # session list. Cheap on the daemon side and exercises the same
    # process the chat path will hit.
    opencode_url = runtime.opencode_client.base_url
    opencode_status: Dict[str, Any]
    opencode_ok: bool
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{opencode_url.rstrip('/')}/session")
        opencode_ok = resp.status_code < 500
        opencode_status = {"base_url": opencode_url, "code": resp.status_code}
    except Exception as exc:  # noqa: BLE001
        opencode_ok = False
        opencode_status = {"base_url": opencode_url, "error": str(exc)}

    result: Dict[str, Any] = {
        "status": "ready" if (ollama_ok and opencode_ok) else "degraded",
        "ollama": ollama_status,
        "opencode": opencode_status,
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
        "opencode_base_url": runtime.opencode_client.base_url,
        "opencode_provider": runtime.opencode_client.provider_id,
        "opencode_model": runtime.opencode_client.model_id,
    }

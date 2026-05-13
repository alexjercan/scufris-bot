"""Bearer-token auth dependency.

Auth is *opt-in*: when ``SCUFRIS_TOKEN`` is unset the dependency is a
no-op so the default localhost-only deployment stays friction-free. When
the env var is set, every request must carry ``Authorization: Bearer
<token>`` matching it.
"""

from __future__ import annotations

import os

from fastapi import Header, HTTPException, status


async def require_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency that enforces bearer auth when configured."""
    expected = os.environ.get("SCUFRIS_TOKEN")
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    if token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

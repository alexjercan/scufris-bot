"""Bearer-token auth dependency.

Auth is *opt-in*: when ``[server].token`` is unset (and ``SCUFRIS_TOKEN``
is also unset) the dependency is a no-op so the default localhost-only
deployment stays friction-free. When a token is configured, every
request must carry ``Authorization: Bearer <token>`` matching it.

The token lives on ``runtime.config.server.token`` — populated from
TOML or overridden by the ``SCUFRIS_TOKEN`` env var (env wins).
"""

from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException, Request, status


def _expected_token(request: Request) -> Optional[str]:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        # Should never happen at request time (lifespan sets it), but
        # be defensive: treat as unauthenticated config.
        return None
    return runtime.config.server.token


async def require_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency that enforces bearer auth when configured."""
    expected = _expected_token(request)
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

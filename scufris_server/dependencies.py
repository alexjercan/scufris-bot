"""FastAPI dependencies for route handlers.

Lives separately from :mod:`scufris_server.app` to break the import
cycle: routes need DI helpers, ``app`` mounts the routers, so the
helpers must live in a module that neither side has to import the
other to reach.

Step 7 only needs the opencode-client dependency. Step 8 (chat) will
add a DB connection dependency here.
"""

from __future__ import annotations

from fastapi import Request

from scufris_server.opencode_client import OpencodeClient


def get_opencode_client(request: Request) -> OpencodeClient:
    """FastAPI dependency: return the lifespan-scoped opencode client.

    Usage::

        from fastapi import Depends
        from scufris_server.dependencies import get_opencode_client

        @router.get("/foo")
        async def foo(oc: OpencodeClient = Depends(get_opencode_client)):
            ...
    """
    client: OpencodeClient = request.app.state.opencode
    return client

"""Scufris HTTP server.

Hosts a single agent runtime in-process and serves multiple concurrent
clients (CLI, future web UI, eventually Telegram) over HTTP. See
``tasks/20260510-192350/DESIGN.md`` for the authoritative design.
"""

from .app import create_app

__all__ = ["create_app"]

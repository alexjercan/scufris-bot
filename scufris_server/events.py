"""Placeholder for the opencode SSE event consumer (task #11 —
``tasks/20260613-091045``: "SSE streaming of opencode 'thinking'
events").

This module exists so dependent tasks can import
``scufris_server.events`` without ``ImportError`` and so the file
structure laid out in #9's design is in place.

Task #11 will turn this into a long-running consumer of opencode's
``GET /event`` SSE stream that fans messages out to per-session
subscribers (driving ``/v1/chat/stream`` and the permission UX in
#30). The session lifecycle is intentionally undefined here: #11
will pick between a global lifespan-spawned task vs. a per-request
generator after profiling.

For now, no symbols are exported. Future tasks can grow this into a
package (``scufris_server/events/``) if multiple modules become
useful.
"""

from __future__ import annotations

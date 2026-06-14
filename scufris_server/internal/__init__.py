"""Placeholder package for the plugin↔scufris-server protocol (task
#32 — ``tasks/20260613-093857``: "Plugin ↔ scufris-server protocol:
facts HTTP contract + auth token model").

This package exists so dependent tasks can import
``scufris_server.internal`` without ``ImportError`` and so the file
structure laid out in #9's design is in place.

Task #32 will turn this into a real package that exposes:

- A FastAPI router for ``/v1/internal/*`` endpoints (facts upsert,
  delete, list, and per-user context for the compaction hook).
- A ``SCUFRIS_PLUGIN_TOKEN`` Bearer-auth dependency, distinct from the
  user-facing ``SCUFRIS_TOKEN``.
- Pydantic schemas published as the source of truth for the in-tree
  TypeScript plugin (``.opencode/plugin/scufris.ts``).

For now, no symbols are exported.
"""

from __future__ import annotations

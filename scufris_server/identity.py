"""Placeholder for the identity-resolution business module (task #12 —
``tasks/20260613-091046``: "Identity layer + XDG user config").

This module exists so dependent tasks can import
``scufris_server.identity`` without ``ImportError`` and so the file
structure laid out in #9's design is in place.

Task #12 will turn this into the v2 carry-over of the v1 identity
layer (``tasks/20260520-145231`` and ``tasks/20260603-132426``):

- Per-user TOML at ``$XDG_CONFIG_HOME/scufris/users/<id>.toml``.
- Surface-binding resolution (CLI, Telegram, opencode-plugin token).
- A pure-Python API for ``routes/identity.py`` to drive
  ``POST /v1/identity/resolve``.

The HTTP layer lives in :mod:`scufris_server.routes.identity` (also
a placeholder until #12).

For now, no symbols are exported.
"""

from __future__ import annotations

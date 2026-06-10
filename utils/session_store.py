"""Persistent map from ``user_id`` to OpenCode ``session_id``.

Survives ``scufris-server`` restarts so users keep their conversational
continuity rather than getting a fresh session every time the daemon
restarts. OpenCode itself stores conversation state per session id;
this map is just the bridge between our integer user ids and those
opaque session ids.

Storage is a tiny JSON file written via atomic rename. Path resolution
(checked in order):

1. ``$SCUFRIS_DATA_DIR`` -- explicit override (tests, ad-hoc deploys).
2. ``$STATE_DIRECTORY`` -- set by systemd's ``StateDirectory=`` in
   production. See ``nix/modules/scufris.nix``.
3. ``<repo>/data`` -- dev fallback, mirrors the convention used by
   ``utils/telemetry.py``.

On-disk format (versioned for future migration to SQLite, etc.)::

    {
      "version": 1,
      "sessions": {
        "<user_id>": "<session_id>",
        ...
      }
    }

JSON object keys are always strings, so user ids are stringified on
write and parsed back to ``int`` on read; entries with non-integer
keys or non-string values are dropped with a logged warning.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("scufris-bot.session_store")

SCHEMA_VERSION = 1
DEFAULT_FILENAME = "opencode_sessions.json"


def _default_data_dir() -> Path:
    """Resolve the on-disk data directory.

    Order: ``$SCUFRIS_DATA_DIR`` > ``$STATE_DIRECTORY`` (systemd) >
    ``<repo>/data``.
    """
    env_explicit = os.environ.get("SCUFRIS_DATA_DIR")
    if env_explicit:
        return Path(env_explicit)
    env_systemd = os.environ.get("STATE_DIRECTORY")
    if env_systemd:
        # systemd sets this to a colon-separated list when multiple
        # StateDirectory= entries are configured; the first is ours.
        first = env_systemd.split(":", 1)[0]
        if first:
            return Path(first)
    return Path(__file__).resolve().parent.parent / "data"


def default_session_store_path() -> Path:
    """Where the session store lives by default. Exposed for diagnostics."""
    return _default_data_dir() / DEFAULT_FILENAME


class SessionStore:
    """Thread-safe persistence for the ``user_id -> session_id`` map.

    The in-memory dict is the source of truth between writes; every
    mutation persists via :meth:`_save_unlocked` (atomic rename).
    Reads return a copy so callers can't bypass the lock.

    The class is sync because the underlying file is tiny (a few
    bytes per user) and writes happen behind ``AgentManager``'s
    ``asyncio.Lock`` -- no event-loop blocking concern at this scale.
    A ``threading.Lock`` is held additionally so that direct callers
    from sync test code (or future threaded paths) remain safe.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        """
        Args:
            path: Override the storage location. ``None`` resolves
                via :func:`default_session_store_path`.
        """
        self._path: Path = path if path is not None else default_session_store_path()
        self._lock = threading.Lock()
        self._sessions: Dict[int, str] = {}
        self._load_unlocked()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def as_dict(self) -> Dict[int, str]:
        """Return a snapshot copy of the current map."""
        with self._lock:
            return dict(self._sessions)

    def get(self, user_id: int) -> Optional[str]:
        with self._lock:
            return self._sessions.get(user_id)

    def set(self, user_id: int, session_id: str) -> None:
        """Insert / update one entry. No-op when the value is unchanged."""
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        with self._lock:
            if self._sessions.get(user_id) == session_id:
                return
            self._sessions[user_id] = session_id
            self._save_unlocked()

    def pop(self, user_id: int) -> Optional[str]:
        """Remove and return the entry for ``user_id``; ``None`` if absent."""
        with self._lock:
            sid = self._sessions.pop(user_id, None)
            if sid is not None:
                self._save_unlocked()
            return sid

    def replace_all(self, mapping: Dict[int, str]) -> None:
        """Atomically replace the whole map (used by prune)."""
        new_map: Dict[int, str] = {}
        for k, v in mapping.items():
            if isinstance(v, str) and v:
                new_map[int(k)] = v
        with self._lock:
            if new_map == self._sessions:
                return
            self._sessions = new_map
            self._save_unlocked()

    # ------------------------------------------------------------------
    # Disk I/O (caller must hold ``self._lock`` for writers)
    # ------------------------------------------------------------------

    def _load_unlocked(self) -> None:
        if not self._path.exists():
            self._sessions = {}
            return
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "SessionStore: failed to read %s (%s); starting empty",
                self._path,
                exc,
            )
            self._sessions = {}
            return

        if not isinstance(data, dict):
            logger.warning(
                "SessionStore: %s has non-object root (%s); starting empty",
                self._path,
                type(data).__name__,
            )
            self._sessions = {}
            return

        version = data.get("version")
        if version != SCHEMA_VERSION:
            logger.warning(
                "SessionStore: %s has version=%r (expected %d); starting empty",
                self._path,
                version,
                SCHEMA_VERSION,
            )
            self._sessions = {}
            return

        raw = data.get("sessions")
        if raw is None:
            self._sessions = {}
            return
        if not isinstance(raw, dict):
            logger.warning(
                "SessionStore: %s 'sessions' is not an object; starting empty",
                self._path,
            )
            self._sessions = {}
            return

        loaded: Dict[int, str] = {}
        for k, v in raw.items():
            try:
                user_id = int(k)
            except (TypeError, ValueError):
                logger.warning(
                    "SessionStore: dropping non-integer user_id key %r in %s",
                    k,
                    self._path,
                )
                continue
            if not isinstance(v, str) or not v:
                logger.warning(
                    "SessionStore: dropping bad session_id %r for user_id %s in %s",
                    v,
                    user_id,
                    self._path,
                )
                continue
            loaded[user_id] = v
        self._sessions = loaded
        logger.info("SessionStore: loaded %d entries from %s", len(loaded), self._path)

    def _save_unlocked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "sessions": {str(k): v for k, v in sorted(self._sessions.items())},
        }
        # Write to a tempfile in the same directory, fsync, then
        # ``os.replace`` (atomic on POSIX within one filesystem).
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=".opencode_sessions.",
            suffix=".tmp",
            dir=str(self._path.parent),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)
        except Exception:
            # Best-effort cleanup; re-raise so the caller learns.
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise

"""User identity layer for scufris-server (#12 — ``20260613-091046``).

Two responsibilities:

1. **TOML loading.** Parse ``~/.config/scufris/config.toml`` into typed
   pydantic models. Single-user shape — matches v1 verbatim per design
   doc §11. Other v1 sub-tables (``[user.schedule]``, ``[user.rag]``,
   ``[user.journal]``, ``[user.notifications]``, ``[server]``) are
   tolerated but ignored; their parsing belongs to future tasks.

2. **Resolution.** Given ``(surface, surface_id)``, return a
   :class:`ResolvedUser` pointing at the right ``users.id``,
   materialising ``surface_bindings`` rows on first contact.

Pure Python — no FastAPI imports. The future scheduler / CLI tools
can call :func:`resolve_user` directly without dragging the HTTP
layer in.

Design references
-----------------
- Design doc ``tasks/20260613-091036/TASK.md`` §11 + §9 line 291.
- v1 carryover tasks ``tasks/20260520-145231`` (original identity
  design) and ``tasks/20260603-132426`` (TOML refactor) — v1 code is
  gone, but the *design* is preserved.
- This task ``tasks/20260613-091046/TASK.md`` for the four locked
  decisions (D1–D4) and additional micro-decisions.

Resolution algorithm
--------------------
:func:`resolve_user` consults sources in this order:

1. **Override** (D4): ``override_user_id`` set → return that user
   verbatim. No TOML, no binding writes.
2. **Existing binding**: ``surface_bindings`` already maps
   ``(surface, surface_id)`` → return that user. Bindings are
   sticky; once materialised, TOML edits don't re-route them.
3. **TOML hit** (D2 + D3): ``identity_file.user`` lists this
   ``surface_id`` under ``identity[surface]`` → ensure the user's
   row exists in ``users``, write a binding, return.
4. **Default fallback** (D1): bind to ``DEFAULT_USER_ID`` (the
   seeded "default" user). Any unknown surface_id ends up here.

Steps 3 and 4 both materialise a ``surface_bindings`` row, so the
*next* call for the same ``(surface, surface_id)`` short-circuits at
step 2.

Logging
-------
- INFO once per first-time bind (TOML hit or default fallback) —
  interesting and infrequent.
- DEBUG on cache hit and on override resolution — frequent and noisy.
- INFO at load time listing any ignored top-level keys ("why isn't
  my [user.schedule] doing anything").
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("scufris_server.identity")

# The default user seeded by lifespan (see ``app._seed_default_user``).
# Module-level constant so app.py and routes/chat.py can both import a
# single source of truth instead of redefining the magic number.
DEFAULT_USER_ID = 1
DEFAULT_USERNAME = "default"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class UserConfig(BaseModel):
    """The ``[user]`` table from ``config.toml``.

    Required: ``username``. Optional: ``identity`` mapping (surface
    name → surface_id). Other v1 sub-tables (``schedule``, ``rag``,
    ``journal``, ``notifications``) are silently dropped — future
    tool tasks will parse them as needed.
    """

    model_config = ConfigDict(extra="ignore")

    username: str
    identity: dict[str, str] = Field(default_factory=dict)


class IdentityFile(BaseModel):
    """Root of ``config.toml``.

    ``user`` is optional: a missing or empty ``config.toml`` is fine
    and means "only the default user resolves". Top-level keys
    outside ``[user]`` (e.g. ``[server]``) are silently ignored.
    """

    model_config = ConfigDict(extra="ignore")

    user: UserConfig | None = None


class BoundSurface(BaseModel):
    """One row from ``surface_bindings``."""

    surface: str
    surface_id: str


class ResolvedUser(BaseModel):
    """Result of :func:`resolve_user`.

    Shape matches design §9 line 291.

    ``bound_surfaces`` is the full list of bindings for ``user_id``
    at resolve-time, sorted ``(surface ASC, surface_id ASC)`` for
    test stability and predictable client rendering. It includes the
    binding we just materialised when this call was a first-time
    hit, so callers always see a complete picture.
    """

    user_id: int
    username: str
    surface: str
    surface_id: str
    bound_surfaces: list[BoundSurface]


# ---------------------------------------------------------------------------
# TOML loading
# ---------------------------------------------------------------------------


def _xdg_config_path() -> Path:
    """Default location for ``config.toml``.

    Honours ``$XDG_CONFIG_HOME`` (XDG Base Directory spec) when set,
    else falls back to ``~/.config``. Always appends
    ``scufris/config.toml``.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "scufris" / "config.toml"


def load_user_identity(path: Path | None = None) -> IdentityFile:
    """Parse ``config.toml`` into an :class:`IdentityFile`.

    Resolution order for ``path``:

    1. Explicit argument wins (used by tests and by
       ``Settings.config_path``).
    2. Otherwise :func:`_xdg_config_path` returns the XDG default.

    Behaviour:

    - Missing file → empty :class:`IdentityFile` (no user defined).
      The caller falls back to the seeded default user for every
      resolve.
    - Malformed TOML → :class:`tomllib.TOMLDecodeError` propagates.
      Lifespan will fail to start; that's intentional — silently
      degrading a typo'd config to "default user only" is worse than
      a loud crash.
    - Schema violation (e.g. ``[user]`` present but ``username``
      missing) → :class:`pydantic.ValidationError` propagates. Same
      reasoning.
    - Unknown top-level keys (``[server]``, ``[scheduler]``, etc.) →
      ignored; logged at INFO so operators can spot stale config.
    """
    target = path if path is not None else _xdg_config_path()
    if not target.is_file():
        logger.info(
            "no config.toml at %s; default user only",
            target,
            extra={"config_path": str(target), "found": False},
        )
        return IdentityFile()

    with target.open("rb") as fh:
        raw = tomllib.load(fh)

    extras = sorted(set(raw.keys()) - {"user"})
    if extras:
        logger.info(
            "ignoring top-level keys in %s: %s",
            target,
            extras,
            extra={"config_path": str(target), "ignored_keys": extras},
        )

    parsed = IdentityFile.model_validate(raw)
    logger.info(
        "loaded config.toml: user=%s",
        parsed.user.username if parsed.user else None,
        extra={
            "config_path": str(target),
            "found": True,
            "username": parsed.user.username if parsed.user else None,
            "surfaces": (sorted(parsed.user.identity.keys()) if parsed.user else []),
        },
    )
    return parsed


# ---------------------------------------------------------------------------
# DB helpers (private)
# ---------------------------------------------------------------------------


def _bound_surfaces(conn: sqlite3.Connection, user_id: int) -> list[BoundSurface]:
    """All ``surface_bindings`` rows for ``user_id``, sorted for stability."""
    rows = conn.execute(
        "SELECT surface, surface_id FROM surface_bindings "
        "WHERE user_id = ? "
        "ORDER BY surface ASC, surface_id ASC",
        (user_id,),
    ).fetchall()
    return [
        BoundSurface(surface=r["surface"], surface_id=r["surface_id"]) for r in rows
    ]


def _username_for(conn: sqlite3.Connection, user_id: int) -> str | None:
    """Look up ``users.username`` by id, or ``None`` if no such row."""
    row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["username"] if row is not None else None


def _ensure_user_row(conn: sqlite3.Connection, username: str) -> int:
    """Return the ``users.id`` for ``username``, inserting the row if absent.

    Used when a TOML user is referenced for the first time — we
    materialise their row in ``users`` lazily, on the first call to
    :func:`resolve_user` that finds them in TOML.

    The ``users.username`` column is ``UNIQUE NOT NULL``; the SELECT
    + conditional INSERT pattern is safe under our single-writer
    SQLite WAL setup. Two concurrent boots of scufris-server against
    the same DB (rare; mostly an edge case during deploys) would
    serialise on the writer lock.
    """
    row = conn.execute(
        "SELECT id FROM users WHERE username = ?", (username,)
    ).fetchone()
    if row is not None:
        return int(row["id"])
    with conn:
        cursor = conn.execute(
            "INSERT INTO users (username, created_at) VALUES (?, ?)",
            (username, int(time.time())),
        )
    # lastrowid is set by sqlite3 immediately after INSERT; it's only
    # None for statements that don't insert. The cast is for mypy.
    new_id = cursor.lastrowid
    assert new_id is not None  # noqa: S101  (post-INSERT invariant)
    return int(new_id)


def _ensure_surface_binding(
    conn: sqlite3.Connection,
    user_id: int,
    surface: str,
    surface_id: str,
) -> bool:
    """Insert ``surface_bindings`` row if absent. Returns True if inserted.

    Idempotent: a second call with the same ``(surface, surface_id)``
    is a no-op and returns False. The PRIMARY KEY on
    ``(surface, surface_id)`` would otherwise cause a UNIQUE
    violation on naïve INSERTs.
    """
    existing = conn.execute(
        "SELECT user_id FROM surface_bindings WHERE surface = ? AND surface_id = ?",
        (surface, surface_id),
    ).fetchone()
    if existing is not None:
        return False
    with conn:
        conn.execute(
            "INSERT INTO surface_bindings (user_id, surface, surface_id) "
            "VALUES (?, ?, ?)",
            (user_id, surface, surface_id),
        )
    return True


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_user(
    conn: sqlite3.Connection,
    surface: str,
    surface_id: str,
    identity_file: IdentityFile,
    override_user_id: int | None = None,
) -> ResolvedUser:
    """Resolve ``(surface, surface_id)`` to a :class:`ResolvedUser`.

    See module docstring for the four-step algorithm. Always returns
    a populated :class:`ResolvedUser`; the only way to get an
    exception out of this function is mis-seeded DB state (an
    override or fallback id that doesn't exist in ``users``).

    Parameters
    ----------
    conn
        Open SQLite connection. Per-request from ``get_db_conn`` in
        production; an in-memory connection in tests.
    surface
        e.g. ``"cli"``, ``"telegram"``, ``"web"``. Free-form string;
        no enum constraint here so future surfaces don't require a
        code change.
    surface_id
        Per-surface id: terminal user, telegram chat_id, web tab id, …
    identity_file
        Pre-loaded TOML config. The caller (FastAPI dependency or
        scheduler) is responsible for loading it once and passing it
        in — this function does no IO of its own apart from SQLite.
    override_user_id
        Server-side override (``SCUFRIS_USER_ID`` env var). When set,
        steps 2–4 are skipped entirely.

    Raises
    ------
    RuntimeError
        Override or fallback resolved to a user_id that doesn't exist
        in the ``users`` table. Indicates a mis-configured deploy:
        either the operator set ``SCUFRIS_USER_ID=N`` for a missing
        user, or the lifespan default-user seed didn't run.
    """
    # 1. Override (D4): ignore everything else, return as-is.
    if override_user_id is not None:
        username = _username_for(conn, override_user_id)
        if username is None:
            raise RuntimeError(
                f"SCUFRIS_USER_ID override points at user_id={override_user_id} "
                "but no such row exists in the users table"
            )
        logger.debug(
            "override resolve: user_id=%d (%s) for (%s, %s)",
            override_user_id,
            username,
            surface,
            surface_id,
            extra={
                "user_id": override_user_id,
                "username": username,
                "surface": surface,
                "surface_id": surface_id,
                "source": "override",
            },
        )
        return ResolvedUser(
            user_id=override_user_id,
            username=username,
            surface=surface,
            surface_id=surface_id,
            bound_surfaces=_bound_surfaces(conn, override_user_id),
        )

    # 2. Existing binding: cache hit; bindings are sticky.
    existing = conn.execute(
        "SELECT user_id FROM surface_bindings WHERE surface = ? AND surface_id = ?",
        (surface, surface_id),
    ).fetchone()
    if existing is not None:
        user_id = int(existing["user_id"])
        username = _username_for(conn, user_id)
        if username is None:
            raise RuntimeError(
                f"surface_bindings row points at user_id={user_id} "
                "but no such row exists in the users table"
            )
        logger.debug(
            "cached resolve: user_id=%d (%s) for (%s, %s)",
            user_id,
            username,
            surface,
            surface_id,
            extra={
                "user_id": user_id,
                "username": username,
                "surface": surface,
                "surface_id": surface_id,
                "source": "cache",
            },
        )
        return ResolvedUser(
            user_id=user_id,
            username=username,
            surface=surface,
            surface_id=surface_id,
            bound_surfaces=_bound_surfaces(conn, user_id),
        )

    # 3. TOML hit (D2/D3): consult config.toml for a match.
    user_cfg = identity_file.user
    if user_cfg is not None and user_cfg.identity.get(surface) == surface_id:
        user_id = _ensure_user_row(conn, user_cfg.username)
        _ensure_surface_binding(conn, user_id, surface, surface_id)
        logger.info(
            "TOML resolve: user_id=%d (%s) for (%s, %s)",
            user_id,
            user_cfg.username,
            surface,
            surface_id,
            extra={
                "user_id": user_id,
                "username": user_cfg.username,
                "surface": surface,
                "surface_id": surface_id,
                "source": "toml",
            },
        )
        return ResolvedUser(
            user_id=user_id,
            username=user_cfg.username,
            surface=surface,
            surface_id=surface_id,
            bound_surfaces=_bound_surfaces(conn, user_id),
        )

    # 4. Default fallback (D1): bind unknown surface_ids to user_id=1.
    username = _username_for(conn, DEFAULT_USER_ID)
    if username is None:
        raise RuntimeError(
            f"default user (id={DEFAULT_USER_ID}) missing from users table; "
            "lifespan seed step is broken"
        )
    _ensure_surface_binding(conn, DEFAULT_USER_ID, surface, surface_id)
    logger.info(
        "default resolve: user_id=%d (%s) for (%s, %s)",
        DEFAULT_USER_ID,
        username,
        surface,
        surface_id,
        extra={
            "user_id": DEFAULT_USER_ID,
            "username": username,
            "surface": surface,
            "surface_id": surface_id,
            "source": "default",
        },
    )
    return ResolvedUser(
        user_id=DEFAULT_USER_ID,
        username=username,
        surface=surface,
        surface_id=surface_id,
        bound_surfaces=_bound_surfaces(conn, DEFAULT_USER_ID),
    )

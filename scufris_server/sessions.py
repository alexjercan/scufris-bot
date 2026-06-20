"""Session-link service layer (#10 — ``20260613-091044``).

Operates on the ``channels`` and ``session_links`` SQLite tables
established by #9 (design doc ``tasks/20260613-091036/TASK.md`` §7).
Sits between the HTTP routes (:mod:`scufris_server.routes.chat`
and :mod:`scufris_server.routes.sessions`) and the raw SQL so
neither layer has to inline the same JOINs / INSERTs.

No FastAPI imports — keeps any background task (the future fork
endpoint in #33, the stale-link pruner in #34) able to call these
functions directly without dragging the HTTP layer in. Pydantic
*is* used for the wire-shape :class:`Channel` model: identity.py
already follows this pattern (``IdentityFile``, ``BoundSurface``,
``ResolvedUser``), so service modules owning the canonical
pydantic shape for their entity is the established convention.

Design references
-----------------
- Design doc ``tasks/20260613-091036/TASK.md`` §7 (schema),
  §8 (HTTP surface), §9.1 (chat create/reuse flow), §9.4 (fork —
  out of #10 scope, in #33), ADR-13 (fork = new channel, never
  destructive).
- This task ``tasks/20260613-091044/TASK.md`` for the six locked
  design decisions (D1–D6) and the open implementation-time
  questions.

What's here
-----------
- :class:`Channel` — pydantic wire model for the
  ``(surface, surface_id, agent)`` triple. Used by the chat route
  (request body shape) and the sessions route (per-row response
  shape). The canonical definition; routes import from here.
- :class:`ChannelRow` — frozen dataclass returned by every read
  function. The contract between this module and the route layer
  (D5).
- :func:`create_channel_link` — atomic INSERT of a ``channels`` +
  ``session_links`` pair. Called from chat when opencode hands us
  a fresh session id; also the insert path the fork endpoint
  (#33) will share.
- :func:`list_user_channels` — every channel a user has, joined
  with its session link, ordered most-recently-used first.
- :func:`get_channel` — single-row lookup by ``channel_id``,
  joined with the link. Returns ``None`` when the channel doesn't
  exist or has no link (orphan channels — see "Joining policy"
  below).
- :func:`clear_channel_link` — drop one session link; True if a
  row was deleted.
- :func:`clear_user_links` — bulk drop for a user; returns the
  deleted count.

Joining policy
--------------
All reads use INNER JOIN between ``channels`` and ``session_links``,
so orphan ``channels`` rows (no link — left over from
:func:`clear_channel_link` / :func:`clear_user_links` per ADR-13)
stay invisible to UI surfaces (no ghost rows in
:func:`list_user_channels`, no false-positive hits in
:func:`get_channel`).

Writes handle orphans transparently: :func:`create_channel_link`
does a lookup-first pass and reuses an existing ``channels.id``
when one already exists for ``(user_id, surface, surface_id,
agent)``, falling back to a fresh INSERT only when no row matches.
That keeps the post-clear "user chats again on the same channel"
path working without a UNIQUE-constraint crash, and means the v0
"orphans don't surface as ghost UI rows" choice still holds for
reads.

Logging
-------
- INFO on writes that change state (create, clear-one, clear-user)
  with ``extra={channel_id, oc_session_id, user_id, ...}`` for
  :class:`~scufris_server.logging.JsonFormatter` lift.
- Read functions stay silent; the request-id middleware already
  ties them to the originating HTTP call.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass

from pydantic import BaseModel, Field

logger = logging.getLogger("scufris_server.sessions")


# ---------------------------------------------------------------------------
# Wire shape
# ---------------------------------------------------------------------------


class Channel(BaseModel):
    """The conversational context a chat message belongs to.

    The triple ``(surface, surface_id, agent)`` (plus ``user_id``,
    derived elsewhere) is the primary key for opencode-session
    reuse: messages with the same channel land in the same opencode
    session, and so share context.

    Lives in the service module so both the chat route (request
    body) and the sessions route (response row) reference the same
    schema; previously sat in :mod:`scufris_server.routes.chat`.
    """

    surface: str = Field(..., min_length=1, description="cli | telegram | web | ...")
    surface_id: str = Field(
        ..., min_length=1, description="chat_id, terminal pid, web tab id"
    )
    agent: str = Field(..., min_length=1, description="which scufris agent persona")


# ---------------------------------------------------------------------------
# Row shape (D5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelRow:
    """One row from the ``channels`` ⨝ ``session_links`` join.

    Field origins:

    - ``channel_id``, ``user_id``, ``surface``, ``surface_id``,
      ``agent`` come from ``channels``.
    - ``oc_session_id``, ``created_at``, ``last_used_at`` come
      from ``session_links``.

    Timestamps are unix seconds (matching the schema in
    ``migrations/001_initial.sql``). Frozen so callers can't
    mutate state under us — the dataclass is a snapshot, not a
    handle.
    """

    channel_id: int
    user_id: int
    surface: str
    surface_id: str
    agent: str
    oc_session_id: str
    created_at: int
    last_used_at: int


def _row_to_channel(row: sqlite3.Row) -> ChannelRow:
    """Materialise a :class:`ChannelRow` from a ``sqlite3.Row``.

    Centralised so the SELECT column list is matched by exactly
    one constructor — adding a column means updating one place,
    not every read function.
    """
    return ChannelRow(
        channel_id=row["channel_id"],
        user_id=row["user_id"],
        surface=row["surface"],
        surface_id=row["surface_id"],
        agent=row["agent"],
        oc_session_id=row["oc_session_id"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
    )


# Shared SELECT clause for :func:`list_user_channels` and
# :func:`get_channel`. Pulled out so the column→ChannelRow mapping
# in :func:`_row_to_channel` stays in sync with the wire shape.
_CHANNEL_SELECT = (
    "SELECT c.id AS channel_id, c.user_id, c.surface, c.surface_id, c.agent, "
    "sl.oc_session_id, sl.created_at, sl.last_used_at "
    "FROM channels c JOIN session_links sl ON sl.channel_id = c.id"
)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_channel_link(
    conn: sqlite3.Connection,
    user_id: int,
    surface: str,
    surface_id: str,
    agent: str,
    oc_session_id: str,
) -> int:
    """Atomically link an opencode session to a channel row.

    Returns the ``channels.id`` — newly minted on first call for a
    given ``(user_id, surface, surface_id, agent)`` tuple, reused on
    subsequent calls after a clear. Both writes (the optional
    ``channels`` insert and the always-required ``session_links``
    insert) share one transaction (``with conn:``) so a crash mid-
    pair leaves the DB unchanged.

    Two flows, picked transparently by an up-front lookup:

    1. **Fresh channel.** No ``channels`` row exists yet for the
       tuple → INSERT both ``channels`` and ``session_links``.
       The chat-arrival happy path.
    2. **Relink after clear.** A ``channels`` row exists but has no
       ``session_links`` row (left over from
       :func:`clear_channel_link` / :func:`clear_user_links` per
       ADR-13 — clears preserve ``channels``, drop only
       ``session_links``). Reuse the existing ``channel_id``,
       INSERT only the ``session_links`` row. The chat-arrival-
       after-clear path.

    The lookup-then-insert is *not* a TOCTOU race in our setup: we
    hold the connection's write lock across the ``with conn:`` block
    (sqlite serialises writes via the rollback journal / WAL writer
    lock) and scufris-server is single-tenant per process. A second
    writer waiting for the lock would see our committed row on its
    own SELECT and take path 2 itself.

    If a ``session_links`` row *already* exists for the channel
    (caller bug — should have called :func:`_resolve_session` and
    seen ``oc_session_id``), the INSERT fails with
    :class:`sqlite3.IntegrityError` on the ``session_links.channel_id``
    PRIMARY KEY. We deliberately don't ``INSERT OR REPLACE`` because
    silently overwriting an existing link would mask the upstream
    bug.

    Replaces the private ``_record_new_session`` helper that lived
    in :mod:`scufris_server.routes.chat` before #10. Chat (and chat-
    stream from #11 step 8, and the future fork endpoint #33)
    delegate here so the relink fix lands in one place.

    Parameters take raw strings rather than the chat layer's
    ``Channel`` pydantic model — keeps this module driver-agnostic
    (no FastAPI / pydantic imports needed).
    """
    now = int(time.time())
    with conn:
        existing = conn.execute(
            "SELECT id FROM channels "
            "WHERE user_id = ? AND surface = ? "
            "AND surface_id = ? AND agent = ?",
            (user_id, surface, surface_id, agent),
        ).fetchone()
        if existing is None:
            cursor = conn.execute(
                "INSERT INTO channels (user_id, surface, surface_id, agent) "
                "VALUES (?, ?, ?, ?)",
                (user_id, surface, surface_id, agent),
            )
            channel_id = cursor.lastrowid
            # sqlite3 always returns the new rowid on a successful single-row
            # INSERT against an INTEGER PRIMARY KEY; the assert is for mypy
            # (lastrowid is typed ``int | None``) and as a defensive
            # tripwire if the contract ever changes.
            assert channel_id is not None, "sqlite3 INSERT did not return lastrowid"
            relinked = False
        else:
            channel_id = int(existing["id"])
            relinked = True
        conn.execute(
            "INSERT INTO session_links "
            "(channel_id, oc_session_id, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?)",
            (channel_id, oc_session_id, now, now),
        )
    logger.info(
        "channel_link relinked" if relinked else "channel_link created",
        extra={
            "channel_id": channel_id,
            "user_id": user_id,
            "oc_session_id": oc_session_id,
            "surface": surface,
            "surface_id": surface_id,
            "agent": agent,
            "relinked": relinked,
        },
    )
    return channel_id


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_user_channels(conn: sqlite3.Connection, user_id: int) -> list[ChannelRow]:
    """Return every linked channel for ``user_id``, most-recent first.

    Sort key is ``session_links.last_used_at DESC`` (open question
    Q3 in TASK.md, resolved: matches the "most recent activity"
    UX users expect; ``created_at`` would pin order at channel-
    creation time which gets stale quickly).

    Empty list when the user has no channels — or has only orphan
    channels (see module-docstring "Joining policy").
    """
    rows = conn.execute(
        f"{_CHANNEL_SELECT} WHERE c.user_id = ? ORDER BY sl.last_used_at DESC",
        (user_id,),
    ).fetchall()
    return [_row_to_channel(r) for r in rows]


def get_channel(conn: sqlite3.Connection, channel_id: int) -> ChannelRow | None:
    """Look up one channel by id, joined with its session link.

    Returns ``None`` for unknown ``channel_id`` *or* for orphan
    channels (no session link — see "Joining policy"). Callers
    that need to distinguish those two cases can run
    ``SELECT id FROM channels WHERE id=?`` directly; no current
    caller does, so the consolidated lookup stays.

    Ownership check is the *caller's* job: this function takes no
    ``user_id`` argument. The clear-one route pairs it with an
    explicit ``if channel.user_id != resolved_user_id: 404`` step
    to avoid leaking channel existence to non-owners (D2).
    """
    row = conn.execute(f"{_CHANNEL_SELECT} WHERE c.id = ?", (channel_id,)).fetchone()
    return _row_to_channel(row) if row else None


# ---------------------------------------------------------------------------
# Clears
# ---------------------------------------------------------------------------


def clear_channel_link(conn: sqlite3.Connection, channel_id: int) -> bool:
    """Drop the ``session_links`` row for one channel.

    The ``channels`` row is preserved — design §9.4 + ADR-13:
    scufris never destroys conversation content (that lives in
    opencode), and the channel row is part of the audit trail of
    "which surfaces this user has chatted from."

    Returns ``True`` when a link row existed and was deleted,
    ``False`` when the channel had no link to clear. The caller's
    HTTP surface translates this to ``{cleared: true|false}`` —
    see the route for the 404-vs-200 decision tree.

    Idempotent: calling twice on the same channel returns ``True``
    then ``False``.
    """
    with conn:
        cursor = conn.execute(
            "DELETE FROM session_links WHERE channel_id = ?", (channel_id,)
        )
    cleared = cursor.rowcount > 0
    if cleared:
        logger.info(
            "channel_link cleared",
            extra={"channel_id": channel_id},
        )
    return cleared


def clear_user_links(conn: sqlite3.Connection, user_id: int) -> int:
    """Drop every ``session_links`` row belonging to one user.

    Returns the number of rows deleted (0 if the user had no
    links). Idempotent: a second call returns 0.

    ``channels`` rows are preserved for the audit-trail reason in
    :func:`clear_channel_link`. The user's next chat on each
    channel will create a fresh ``session_links`` row pointing at
    a newly-allocated opencode session.

    Uses a subquery rather than a JOIN-DELETE (sqlite doesn't
    support ``DELETE ... USING``) so the delete is portable and
    obvious in EXPLAIN.
    """
    with conn:
        cursor = conn.execute(
            "DELETE FROM session_links "
            "WHERE channel_id IN ("
            "SELECT id FROM channels WHERE user_id = ?"
            ")",
            (user_id,),
        )
    count = cursor.rowcount
    if count > 0:
        logger.info(
            "user_links cleared",
            extra={"user_id": user_id, "count": count},
        )
    return count

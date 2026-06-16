"""HTTP layer for per-user opencode session management
(task #10 — ``tasks/20260613-091044``).

Three endpoints across two routers (D4):

- :data:`sessions_router` (prefix ``/v1/sessions``) — owns the
  per-channel surface:

    * ``GET /v1/sessions``               — implemented here (#10).
    * ``POST /v1/sessions/{channel_id}/clear`` — added in step 8.

- :data:`clear_router` (prefix ``/v1``) — owns the user-wide
  cleanup endpoint:

    * ``POST /v1/clear``                 — added in step 9.

Both are mounted by :mod:`scufris_server.routes` (step 10).

Authz model (D2)
----------------
No bearer auth in v0. Resolution is *override-first*:

1. ``SCUFRIS_USER_ID`` env override (when set, all requests are
   pinned to that user_id; query / body ``user_id`` is ignored).
2. Caller-supplied ``user_id`` (validated against ``users``;
   404 if unknown).
3. Default user (``DEFAULT_USER_ID = 1``) when no override and
   no caller-supplied id.

Per-channel ``clear`` additionally enforces
``channels.user_id == resolved_user_id`` (404 leaks no info to
non-owners).

Opencode-degradation contract (D1)
----------------------------------
:func:`list_sessions` calls :meth:`OpencodeClient.list_sessions`
to enrich each row with ``title`` / ``tokens`` / ``cost``. When
opencode is unreachable (network error or 5xx) the response
*degrades* — every enriched field is null and a WARNING is logged
— rather than failing with a 503. The motivation: the scufris-
side metadata (``channel_id``, ``oc_session_id``, ``last_used_at``)
remains useful even when opencode is down (e.g. so the operator
can still ``/v1/clear`` an out-of-control link). 4xx responses
from opencode (``OpencodeClientError``) are *not* caught — they
indicate a bug in our request shape, deferred to FastAPI's 500
default.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from scufris_server.dependencies import (
    get_db_conn,
    get_identity_override,
    get_opencode_client,
)
from scufris_server.identity import DEFAULT_USER_ID
from scufris_server.opencode_client import (
    OpencodeClient,
    OpencodeNetworkError,
    OpencodeServerError,
    Session,
)
from scufris_server.routes.chat import Tokens
from scufris_server.sessions import (
    Channel,
    ChannelRow,
    clear_channel_link,
    clear_user_links,
    list_user_channels,
)

sessions_router = APIRouter(prefix="/v1/sessions", tags=["sessions"])
clear_router = APIRouter(prefix="/v1", tags=["sessions"])
logger = logging.getLogger("scufris_server.routes.sessions")


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SessionListItem(BaseModel):
    """One row of ``GET /v1/sessions``.

    Scufris-side fields (``channel_id``, ``channel``, ``oc_session_id``,
    ``last_used_at``) are always present. Opencode-supplied fields
    (``title``, ``tokens``, ``cost``) are nullable per the D1
    degradation contract — when opencode is unreachable, every row
    has them null but the rest of the row is still useful.

    ``last_used_at`` is unix seconds (matches the schema).
    """

    channel_id: int
    channel: Channel
    oc_session_id: str
    last_used_at: int
    title: str | None = None
    tokens: Tokens | None = None
    cost: float | None = None


class ClearResponse(BaseModel):
    """Response body for ``POST /v1/sessions/{channel_id}/clear``.

    ``cleared`` is True when a session_link row was removed, False
    when no link existed for the channel (already cleared, or a
    fresh channel with no chat yet). 200 in both cases — see the
    route docstring for the idempotency story.
    """

    cleared: bool


class BulkClearRequest(BaseModel):
    """Body for ``POST /v1/clear``."""

    user_id: int = Field(..., ge=1, description="user whose session_links to drop")


class BulkClearResponse(BaseModel):
    """Response body for ``POST /v1/clear``.

    ``count`` is the number of ``session_links`` rows deleted.
    ``count == 0`` is a successful no-op (D6) — the user exists
    but had no live links to clear; not a 404.
    """

    count: int


# ---------------------------------------------------------------------------
# Authz helper
# ---------------------------------------------------------------------------


def _resolve_principal(
    conn: sqlite3.Connection,
    override: int | None,
    requested_user_id: int | None,
) -> int:
    """Apply the override-first authz rule (D2) and return a user_id.

    Order of precedence:

    1. Process-wide override (``SCUFRIS_USER_ID`` env). If set, the
       lifespan has already validated the user exists; we trust it
       and return immediately. Caller-supplied ``user_id`` is
       silently ignored — operators running with override active
       are explicitly opting into pinning all requests.
    2. Caller-supplied ``requested_user_id``. We confirm the row
       exists in ``users``; absent rows raise 404 (don't leak
       whether the id was malformed vs unallocated).
    3. Default user (id=1, seeded at lifespan startup). The
       fall-through for "anonymous" callers — same shape as the
       chat-route default-fallback when no surface_binding hits.

    Raises
    ------
    HTTPException(404)
        ``requested_user_id`` is non-None and doesn't match a row.
    """
    if override is not None:
        return override

    if requested_user_id is not None:
        row = conn.execute(
            "SELECT id FROM users WHERE id = ?", (requested_user_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"user_id {requested_user_id} not found",
            )
        return requested_user_id

    return DEFAULT_USER_ID


# ---------------------------------------------------------------------------
# Enrichment helper
# ---------------------------------------------------------------------------


def _build_item(channel: ChannelRow, enriched: Session | None) -> SessionListItem:
    """Materialise one ``SessionListItem`` from the DB row + opencode snap.

    ``enriched`` is the ``Session`` object opencode returned for this
    ``oc_session_id``, or ``None`` when:

    - opencode was unreachable at request time (D1 degradation), or
    - opencode no longer knows about this session (deleted out of
      band; we treat it the same as unreachable).

    The split keeps :func:`list_sessions` readable: the loop over
    channels stays linear and the conditional logic per row is in
    one place.
    """
    tokens = (
        Tokens(input=enriched.tokens.input, output=enriched.tokens.output)
        if enriched is not None and enriched.tokens is not None
        else None
    )
    return SessionListItem(
        channel_id=channel.channel_id,
        channel=Channel(
            surface=channel.surface,
            surface_id=channel.surface_id,
            agent=channel.agent,
        ),
        oc_session_id=channel.oc_session_id,
        last_used_at=channel.last_used_at,
        title=enriched.title if enriched is not None else None,
        tokens=tokens,
        cost=enriched.cost if enriched is not None else None,
    )


# ---------------------------------------------------------------------------
# GET /v1/sessions
# ---------------------------------------------------------------------------


@sessions_router.get("", response_model=list[SessionListItem])
async def list_sessions(
    conn: Annotated[sqlite3.Connection, Depends(get_db_conn)],
    client: Annotated[OpencodeClient, Depends(get_opencode_client)],
    override: Annotated[int | None, Depends(get_identity_override)],
    user_id: int | None = None,
) -> list[SessionListItem]:
    """List every linked channel for the resolved user, most-recent first.

    Query parameters
    ----------------
    user_id : int, optional
        Caller-supplied principal. Ignored when ``SCUFRIS_USER_ID``
        override is active (D2). Falls back to ``DEFAULT_USER_ID``
        (the seeded default user) when both are absent.

    Response
    --------
    Empty list ``[]`` when the user has no linked channels — *not*
    a 404. 404 is reserved for "user_id doesn't exist" so callers
    can distinguish "valid user, no sessions" from "wrong user."

    Behaviour around opencode
    -------------------------
    Per D1: every row's scufris-side fields are always populated.
    Opencode-supplied fields (``title``, ``tokens``, ``cost``) are
    null when opencode is unreachable (network error or 5xx) — the
    failure is logged at WARNING and the response still 200s.
    Failures here don't propagate to the caller because the typical
    operator workflow ("show me my sessions so I can clear stale
    ones") needs the channel ids more than it needs the metadata.
    """
    principal = _resolve_principal(conn, override, user_id)
    channels = list_user_channels(conn, principal)
    if not channels:
        return []

    # Try to enrich with opencode metadata. D1: degrade to nulls on
    # any reachability failure rather than raising 503 — see the
    # module docstring for rationale.
    oc_sessions: dict[str, Session] = {}
    try:
        for s in await client.list_sessions():
            oc_sessions[s.id] = s
    except (OpencodeNetworkError, OpencodeServerError) as exc:
        logger.warning(
            "list_sessions: opencode unavailable; degrading to nulls",
            extra={
                "error": str(exc),
                "error_type": type(exc).__name__,
                "user_id": principal,
                "channel_count": len(channels),
            },
        )

    return [_build_item(c, oc_sessions.get(c.oc_session_id)) for c in channels]


# ---------------------------------------------------------------------------
# POST /v1/sessions/{channel_id}/clear
# ---------------------------------------------------------------------------


@sessions_router.post("/{channel_id}/clear", response_model=ClearResponse)
async def clear_session(
    channel_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_db_conn)],
    override: Annotated[int | None, Depends(get_identity_override)],
) -> ClearResponse:
    """Drop the opencode ``session_link`` for one channel; preserve the channel row.

    Per-channel clear takes no request body — ``channel_id`` is the
    path parameter and the principal is *implicit*:

    - ``SCUFRIS_USER_ID`` env override if set (pins every request
      to one user_id; see :func:`_resolve_principal`).
    - Otherwise ``DEFAULT_USER_ID`` (the seeded default user).

    No caller-supplied user_id: ownership is enforced against the
    channel row's ``user_id`` column. A channel that doesn't exist
    *or* that belongs to a different principal both 404 with the
    same body — we don't leak channel existence to non-owners
    (D2).

    Idempotency
    -----------
    - First call on a live link: ``{cleared: true}``.
    - Second call (or first call on a channel that was never
      chatted): ``{cleared: false}`` — the channels row still
      exists, so the ownership check still passes; only the link
      is gone.

    The ownership lookup queries the ``channels`` table *directly*
    rather than calling :func:`scufris_server.sessions.get_channel`,
    which INNER JOINs and would hide the post-clear state. That
    direct query is the minimum needed to keep the
    ``cleared=false`` idempotent case observable; promoting it to
    a service helper waits for a second caller (#33 fork's
    ownership check may want it).

    Per ADR-13: scufris never destroys the opencode session itself
    here — only our local link. ``GET /session`` against opencode
    still returns the conversation; the next chat to this channel
    allocates a fresh opencode session.
    """
    principal = _resolve_principal(conn, override, None)

    row = conn.execute(
        "SELECT user_id FROM channels WHERE id = ?", (channel_id,)
    ).fetchone()
    if row is None or row["user_id"] != principal:
        logger.warning(
            "clear_session: channel not found or not owned",
            extra={"channel_id": channel_id, "principal": principal},
        )
        raise HTTPException(status_code=404, detail=f"channel {channel_id} not found")

    cleared = clear_channel_link(conn, channel_id)
    logger.info(
        "clear_session: %s",
        "cleared" if cleared else "no-op (already empty)",
        extra={
            "channel_id": channel_id,
            "principal": principal,
            "cleared": cleared,
        },
    )
    return ClearResponse(cleared=cleared)


# ---------------------------------------------------------------------------
# POST /v1/clear
# ---------------------------------------------------------------------------


@clear_router.post("/clear", response_model=BulkClearResponse)
async def clear_user(
    payload: BulkClearRequest,
    conn: Annotated[sqlite3.Connection, Depends(get_db_conn)],
    override: Annotated[int | None, Depends(get_identity_override)],
) -> BulkClearResponse:
    """Drop every ``session_link`` for the resolved user; preserve channel rows.

    Unlike the per-channel clear, this endpoint takes a request
    body containing the target ``user_id``. Authz still follows
    the override-first rule (D2):

    - When ``SCUFRIS_USER_ID`` is set, ``payload.user_id`` is
      silently ignored and the override wins. Operators running
      pinned can't accidentally clear another user from a stray
      curl.
    - Otherwise the supplied ``user_id`` is validated against
      ``users``; 404 when absent.

    Response semantics (D6)
    -----------------------
    - ``count > 0``: that many ``session_links`` rows were
      removed.
    - ``count == 0``: the user exists but had no live links to
      clear. **This is a success**, not a 404 — the caller's
      intent ("there should be no live links for this user") is
      satisfied. 404 is reserved for "the user_id you sent
      doesn't exist."

    Per ADR-13: only our local links are dropped; the underlying
    opencode sessions remain queryable via ``GET /session`` on the
    opencode side until they age out naturally.
    """
    principal = _resolve_principal(conn, override, payload.user_id)
    count = clear_user_links(conn, principal)
    logger.info(
        "clear_user: dropped %d session_link(s)",
        count,
        extra={"principal": principal, "count": count},
    )
    return BulkClearResponse(count=count)

"""``POST /v1/chat`` — synchronous single-turn chat (step 8 of #9).

Per design §9.1 this is the v0 chat path: a JSON-in / JSON-out call
that blocks until opencode returns the assistant's full reply. SSE /
streaming variants are deferred to #11; tool-call surfacing and
permissions live in #30.

Identity resolution (#12) runs once per request before session
lookup: :func:`scufris_server.identity.resolve_user` consults the
``SCUFRIS_USER_ID`` override, the existing ``surface_bindings``
cache, the loaded ``config.toml``, and finally the seeded default
user — in that order. The resolved ``user_id`` is what
``channels`` rows are keyed by, so chat sessions for the same
``(surface, surface_id, agent)`` triple stay pinned to the right
user across surfaces and restarts.

Error policy
------------
The endpoint returns 503 with ``{error, error_type}`` when:

- ``app.state.opencode_default_model`` is ``None`` (opencode has no
  connected provider with a default; lifespan logged this at boot).
- Any opencode call (``create_session`` or ``send_message``) raises
  :class:`OpencodeNetworkError` or :class:`OpencodeServerError`.

4xx responses from opencode (``OpencodeClientError``) are *not*
caught here — they indicate a bug in *our* request shape, so we let
FastAPI surface them as 500s for the operator to chase.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from scufris_server.dependencies import (
    get_db_conn,
    get_identity_override,
    get_opencode_client,
    get_user_identity,
)
from scufris_server.identity import IdentityFile, resolve_user
from scufris_server.opencode_client import (
    OpencodeClient,
    OpencodeNetworkError,
    OpencodeServerError,
    SendMessageRequest,
    TextPartInput,
)
from scufris_server.sessions import Channel, create_channel_link

router = APIRouter(prefix="/v1", tags=["chat"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    """Body of ``POST /v1/chat``."""

    message: str = Field(..., min_length=1, description="user prompt text")
    channel: Channel


class Tokens(BaseModel):
    """Token usage for the turn, as reported by the model."""

    input: int = 0
    output: int = 0


class ChatResponse(BaseModel):
    """Body of a successful ``POST /v1/chat``."""

    reply: str
    oc_session_id: str
    oc_message_id: str
    tokens: Tokens
    cost: float


class ChatErrorBody(BaseModel):
    """Structured 503 body when opencode is unavailable or misconfigured."""

    error: str
    error_type: Literal[
        "DefaultModelMissing",
        "OpencodeNetworkError",
        "OpencodeServerError",
    ]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _resolve_session(
    conn: sqlite3.Connection, user_id: int, channel: Channel
) -> str | None:
    """Return the cached opencode session id for this channel, or ``None``.

    Joins ``channels`` × ``session_links``. There's at most one match —
    the ``channels`` UNIQUE constraint guarantees it.

    NB: this lookup is by ``(user_id, surface, surface_id, agent)`` —
    the chat-arrival shape — and stays here because no other caller
    needs that lookup form. :mod:`scufris_server.sessions` provides
    only the ``channel_id``-keyed :func:`get_channel`, used by the
    sessions routes (#10) and the future fork endpoint (#33).
    """
    row = conn.execute(
        "SELECT sl.oc_session_id "
        "FROM channels c JOIN session_links sl ON sl.channel_id = c.id "
        "WHERE c.user_id = ? AND c.surface = ? "
        "AND c.surface_id = ? AND c.agent = ?",
        (user_id, channel.surface, channel.surface_id, channel.agent),
    ).fetchone()
    return row["oc_session_id"] if row is not None else None


def _touch_session(conn: sqlite3.Connection, oc_session_id: str) -> None:
    """Bump ``last_used_at`` on a reused session. Tiny commit; bounded latency."""
    with conn:
        conn.execute(
            "UPDATE session_links SET last_used_at = ? WHERE oc_session_id = ?",
            (int(time.time()), oc_session_id),
        )


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def _raise_503(error_type: str, message: str) -> NoReturn:
    """Raise HTTPException(503) with our structured body shape."""
    body: dict[str, str] = {"error": message, "error_type": error_type}
    raise HTTPException(status_code=503, detail=body)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    request: Request,
    client: Annotated[OpencodeClient, Depends(get_opencode_client)],
    conn: Annotated[sqlite3.Connection, Depends(get_db_conn)],
    identity_file: Annotated[IdentityFile, Depends(get_user_identity)],
    override: Annotated[int | None, Depends(get_identity_override)],
) -> ChatResponse:
    """Synchronously route a user message through opencode and return the reply.

    Algorithm (design §9.1, with #12 identity carryover):

    1. Verify a default model is cached. If not → 503.
    2. Resolve the request's ``(surface, surface_id)`` to a user
       (TOML, override, or default fallback). Materialises a
       ``surface_bindings`` row on first contact.
    3. Look up an existing opencode session for ``(user_id, channel)``.
    4. If none, ask opencode to ``create_session`` and persist channel
       + session link rows.
    5. Send the user's message via ``send_message``; the call blocks
       until opencode finishes the turn (tool calls included).
    6. Return reply text + the metadata the design exposes.
    """
    default_model = request.app.state.opencode_default_model
    if default_model is None:
        logger.warning(
            "chat 503: no default model cached",
            extra={"channel": payload.channel.model_dump()},
        )
        _raise_503(
            "DefaultModelMissing",
            "opencode has no connected provider with a default model",
        )

    # Identity resolution (#12). Always returns a populated
    # ResolvedUser; raises only on mis-seeded DB state, which would
    # be a 500 (operator-visible bug).
    resolved = resolve_user(
        conn,
        payload.channel.surface,
        payload.channel.surface_id,
        identity_file,
        override_user_id=override,
    )
    user_id = resolved.user_id
    oc_session_id = _resolve_session(conn, user_id, payload.channel)

    if oc_session_id is None:
        # New channel: create the opencode session, then persist.
        try:
            session = await client.create_session(agent=payload.channel.agent)
        except OpencodeNetworkError as exc:
            logger.warning(
                "chat 503: create_session network error: %s",
                exc,
                extra={"error": str(exc), "error_type": "OpencodeNetworkError"},
            )
            _raise_503("OpencodeNetworkError", str(exc))
        except OpencodeServerError as exc:
            logger.warning(
                "chat 503: create_session 5xx: %s",
                exc,
                extra={
                    "error": str(exc),
                    "error_type": "OpencodeServerError",
                    "status": exc.status_code,
                },
            )
            _raise_503("OpencodeServerError", str(exc))

        oc_session_id = session.id
        create_channel_link(
            conn,
            user_id,
            payload.channel.surface,
            payload.channel.surface_id,
            payload.channel.agent,
            oc_session_id,
        )
        logger.info(
            "chat: new opencode session created",
            extra={
                "oc_session_id": oc_session_id,
                "channel": payload.channel.model_dump(),
            },
        )
    else:
        _touch_session(conn, oc_session_id)
        logger.info(
            "chat: reusing opencode session",
            extra={
                "oc_session_id": oc_session_id,
                "channel": payload.channel.model_dump(),
            },
        )

    # Send the user's message.
    send_req = SendMessageRequest(
        parts=[TextPartInput(text=payload.message)],
        model=default_model,
        agent=payload.channel.agent,
    )
    try:
        assistant = await client.send_message(oc_session_id, send_req)
    except OpencodeNetworkError as exc:
        logger.warning(
            "chat 503: send_message network error: %s",
            exc,
            extra={"error": str(exc), "error_type": "OpencodeNetworkError"},
        )
        _raise_503("OpencodeNetworkError", str(exc))
    except OpencodeServerError as exc:
        logger.warning(
            "chat 503: send_message 5xx: %s",
            exc,
            extra={
                "error": str(exc),
                "error_type": "OpencodeServerError",
                "status": exc.status_code,
            },
        )
        _raise_503("OpencodeServerError", str(exc))

    info = assistant.info
    tokens_in = info.tokens.input if info.tokens else 0
    tokens_out = info.tokens.output if info.tokens else 0

    return ChatResponse(
        reply=assistant.text(),
        oc_session_id=oc_session_id,
        oc_message_id=info.id,
        tokens=Tokens(input=tokens_in, output=tokens_out),
        cost=info.cost or 0.0,
    )

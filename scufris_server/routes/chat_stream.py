"""``POST /v1/chat/stream`` — SSE streaming chat (step 8 of #11).

Streaming counterpart to ``POST /v1/chat`` (#9). Same request body
(:class:`ChatRequest`, cross-imported from :mod:`routes.chat` per
#11 D2 + D6), same pre-stream error policy (JSON 503 with structured
body before any SSE bytes are committed), but the reply is streamed
as an SSE event sequence so callers can render opencode's tool calls
and reasoning live instead of waiting for the synchronous send-
message HTTP round-trip to complete.

Wire shape
----------
The response is ``Content-Type: text/event-stream``. Three event
types may appear (one per SSE record):

- ``event: thinking`` — a :class:`scufris_server.events.ThinkingEvent`
  payload (kind ∈ {text, tool_call, tool_result, tool_meta,
  compaction}). Zero or more per stream.
- ``event: done`` — terminal success event. Payload mirrors
  :class:`scufris_server.routes.chat.ChatResponse` plus a literal
  ``type: "done"`` field (#11 D7). Exactly one per successful
  stream.
- ``event: error`` — terminal failure event. Payload
  ``{error: str, error_type: str}``. Mutually exclusive with
  ``done``.

Plus periodic ``: keepalive\\n\\n`` SSE comment lines every
:data:`scufris_server.sse.KEEPALIVE_SECONDS` seconds during idle
periods, injected by :func:`stream_with_keepalive`.

Algorithm (per #11 D3 + D8)
---------------------------
1. Pre-stream JSON validations (default model present, session
   resolve / create). Failures raise :class:`HTTPException(503)`
   *before* any SSE bytes go out — same body shape as ``/v1/chat``.
2. ``async with bus.subscribe(oc_session_id) as queue:`` — register
   the per-session queue on the process-wide :class:`EventBus`.
3. ``post_task = asyncio.create_task(_run_post(...))`` — fire the
   synchronous ``send_message`` POST in the background. The
   wrapper puts a :class:`_PostDone` / :class:`_PostError` sentinel
   onto the same queue when the POST resolves; this is what
   *terminates* the stream (not ``session.idle``, which gets dropped
   by the event mapping layer as a non-user-visible signal).
4. Drain the queue. For each item:
   - :class:`_PostDone` → emit ``done`` SSE event, return.
   - :class:`_PostError` → emit ``error`` SSE event, return.
   - :class:`_BusReconnected` → emit ``error`` SSE event with
     ``error_type: "BusReconnected"``, return (#11 D4 — turn-level
     fail since we may have missed events during the reconnect
     window).
   - ``dict`` → run through :func:`map_opencode_event`; if a
     :class:`ThinkingEvent` falls out, emit ``thinking``; else drop.
5. ``finally:`` cancel ``post_task`` if still in flight. Bus
   unsubscribe is handled by the context manager.

Cross-module imports
--------------------
Per #11 D6 we directly import a handful of chat-route helpers from
:mod:`scufris_server.routes.chat` rather than promoting them to
:mod:`scufris_server.sessions`:

- :class:`ChatRequest`, :class:`ChatErrorBody`, :class:`Tokens` —
  shared wire shapes.
- :func:`_resolve_session`, :func:`_touch_session`, :func:`_raise_503`
  — chat-loop helpers.

Both routes use the same lookup form ``(user_id, surface,
surface_id, agent)`` and the same ``last_used_at`` bump policy, so
sharing the implementations keeps drift impossible. If a third
caller materialises later (e.g. ``POST /v1/chat/regenerate`` in
#33) we promote the helpers to a neutral module then.

Divergences from ``/v1/chat``
-----------------------------
- :class:`OpencodeClientError` (any 4xx from ``send_message``) is
  caught and surfaced as an ``error`` SSE event rather than letting
  it propagate to FastAPI as a 500. Once we've returned the
  :class:`StreamingResponse`, the HTTP headers are already on the
  wire and there's no way to switch to a 5xx response — so all post-
  stream-commit errors must be tunnelled through ``event: error``.
- 404 on ``send_message`` is reported with ``error_type:
  "OpencodeStaleSessionError"`` (the type defined in
  :mod:`scufris_server.opencode_client` for this future-proofing
  case) rather than the generic ``OpencodeClientError`` label, so
  clients can decide whether to drop the local ``session_links`` row
  and retry. The handler does *not* retry in-line — that's the
  caller's policy decision.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from starlette.responses import StreamingResponse

from scufris_server.dependencies import (
    get_db_conn,
    get_event_bus,
    get_identity_override,
    get_opencode_client,
    get_user_identity,
)
from scufris_server.event_mapping import EventMapperState, map_opencode_event
from scufris_server.events import EventBus
from scufris_server.identity import IdentityFile, resolve_user
from scufris_server.opencode_client import (
    AssistantMessage,
    OpencodeClient,
    OpencodeClientError,
    OpencodeNetworkError,
    OpencodeServerError,
    OpencodeUnavailable,
    SendMessageRequest,
    TextPartInput,
    _BusReconnected,
)
from scufris_server.routes.chat import (
    ChatRequest,
    _raise_503,
    _resolve_session,
    _touch_session,
)
from scufris_server.sessions import create_channel_link
from scufris_server.sse import format_event, stream_with_keepalive

router = APIRouter(prefix="/v1", tags=["chat"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-private sentinels (driven onto the bus queue by ``_run_post``)
# ---------------------------------------------------------------------------
#
# The bus queue normally carries raw opencode event dicts plus the
# :class:`_BusReconnected` sentinel. We piggy-back two of our own
# sentinels onto the same queue so the drain loop has a single
# terminal-signal source. Per :meth:`EventBus.subscribe`'s docstring
# subscribers are permitted to put their own sentinels onto the queue;
# the bus never reads from it.


@dataclass(frozen=True)
class _PostDone:
    """The background ``send_message`` POST returned successfully.

    Carries the parsed :class:`AssistantMessage` so the drain loop
    can build the ``done`` SSE payload (reply text, tokens, cost,
    message id).
    """

    assistant: AssistantMessage


@dataclass(frozen=True)
class _PostError:
    """The background ``send_message`` POST raised an exception.

    ``error_type`` is one of the labels we expose on the SSE
    ``error`` event (``OpencodeNetworkError``, ``OpencodeServerError``,
    ``OpencodeStaleSessionError``, ``OpencodeClientError``). ``message``
    is the human-readable detail. ``status`` is the HTTP status code
    when available (4xx/5xx paths), else ``None`` (network errors).
    """

    error_type: str
    message: str
    status: int | None = None


# ---------------------------------------------------------------------------
# Background poster
# ---------------------------------------------------------------------------


async def _run_post(
    client: OpencodeClient,
    oc_session_id: str,
    send_req: SendMessageRequest,
    queue: asyncio.Queue[Any],
) -> None:
    """Run the synchronous ``send_message`` POST and post a sentinel.

    Always terminates with exactly one sentinel onto ``queue``: a
    :class:`_PostDone` on success or a :class:`_PostError` on any
    expected opencode-side failure mode. Unexpected exceptions
    (programmer bugs in this module) propagate to the asyncio
    default-exception handler — the drain loop will eventually time
    out waiting for a sentinel and the route handler's ``finally``
    will cancel the task. We don't catch :class:`Exception` blanket
    because that would hide unrelated bugs.
    """
    try:
        assistant = await client.send_message(oc_session_id, send_req)
    except OpencodeNetworkError as exc:
        await queue.put(_PostError(error_type="OpencodeNetworkError", message=str(exc)))
        return
    except OpencodeServerError as exc:
        await queue.put(
            _PostError(
                error_type="OpencodeServerError",
                message=str(exc),
                status=exc.status_code,
            )
        )
        return
    except OpencodeClientError as exc:
        # 404 is the "stale session" case — opencode GC'd the session
        # out from under us mid-turn (rare; documented in the
        # OpencodeStaleSessionError docstring). All other 4xx codes
        # indicate a malformed request from our side — still surfaced
        # as ``error`` (we can't 500 mid-stream) but with a different
        # label so the client can distinguish.
        error_type = (
            "OpencodeStaleSessionError"
            if exc.status_code == 404
            else "OpencodeClientError"
        )
        await queue.put(
            _PostError(
                error_type=error_type,
                message=str(exc),
                status=exc.status_code,
            )
        )
        return
    await queue.put(_PostDone(assistant=assistant))


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    client: Annotated[OpencodeClient, Depends(get_opencode_client)],
    conn: Annotated[sqlite3.Connection, Depends(get_db_conn)],
    identity_file: Annotated[IdentityFile, Depends(get_user_identity)],
    override: Annotated[int | None, Depends(get_identity_override)],
    bus: Annotated[EventBus, Depends(get_event_bus)],
) -> StreamingResponse:
    """Stream a chat turn as SSE.

    Pre-stream flow (synchronous; can 503):

    1. Verify ``app.state.opencode_default_model`` is cached → 503
       ``DefaultModelMissing`` otherwise.
    2. Resolve identity via :func:`resolve_user` (#12 semantics:
       override > TOML > existing binding > default).
    3. Look up an existing ``session_links`` row for
       ``(user_id, channel)``; if missing, ``create_session`` +
       ``create_channel_link``. Both opencode failures translate to
       503 with the same structured body ``/v1/chat`` returns.

    Stream flow (StreamingResponse generator):

    4. Subscribe to the event bus for ``oc_session_id``.
    5. Fire the background ``send_message`` POST.
    6. Drain the queue; map opencode events to ``thinking`` SSE
       events; terminate on the poster's sentinel or on bus reconnect.
    7. ``finally:`` cancel the poster if still in flight.

    See the module docstring for the full event taxonomy and the
    rationale for the divergences from :func:`routes.chat.chat`.
    """
    default_model = request.app.state.opencode_default_model
    if default_model is None:
        logger.warning(
            "chat/stream 503: no default model cached",
            extra={"channel": payload.channel.model_dump()},
        )
        _raise_503(
            "DefaultModelMissing",
            "opencode has no connected provider with a default model",
        )

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
        try:
            session = await client.create_session(agent=payload.channel.agent)
        except OpencodeNetworkError as exc:
            logger.warning(
                "chat/stream 503: create_session network error: %s",
                exc,
                extra={"error": str(exc), "error_type": "OpencodeNetworkError"},
            )
            _raise_503("OpencodeNetworkError", str(exc))
        except OpencodeServerError as exc:
            logger.warning(
                "chat/stream 503: create_session 5xx: %s",
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
            "chat/stream: new opencode session created",
            extra={
                "oc_session_id": oc_session_id,
                "channel": payload.channel.model_dump(),
            },
        )
    else:
        _touch_session(conn, oc_session_id)
        logger.info(
            "chat/stream: reusing opencode session",
            extra={
                "oc_session_id": oc_session_id,
                "channel": payload.channel.model_dump(),
            },
        )

    send_req = SendMessageRequest(
        parts=[TextPartInput(text=payload.message)],
        model=default_model,
        agent=payload.channel.agent,
    )
    # Pin the resolved id to a local — the closure below captures it
    # and mypy --strict otherwise sees the outer ``oc_session_id``
    # name as ``str | None`` because of the if/else assignment above.
    session_id = oc_session_id

    async def event_source() -> AsyncIterator[bytes]:
        """Inner generator: subscribe, drive POST, drain, emit SSE bytes."""
        state = EventMapperState()
        post_task: asyncio.Task[None] | None = None
        try:
            try:
                async with bus.subscribe(session_id) as queue:
                    post_task = asyncio.create_task(
                        _run_post(client, session_id, send_req, queue),
                        name=f"scufris-chat-stream-post-{session_id[:8]}",
                    )
                    while True:
                        item = await queue.get()

                        if isinstance(item, _PostDone):
                            info = item.assistant.info
                            tokens_in = info.tokens.input if info.tokens else 0
                            tokens_out = info.tokens.output if info.tokens else 0
                            yield format_event(
                                "done",
                                {
                                    "type": "done",
                                    "message": item.assistant.text(),
                                    "oc_session_id": session_id,
                                    "oc_message_id": info.id,
                                    "tokens": {
                                        "input": tokens_in,
                                        "output": tokens_out,
                                    },
                                    "cost": info.cost or 0.0,
                                },
                            )
                            return

                        if isinstance(item, _PostError):
                            logger.warning(
                                "chat/stream: post_task failed: %s (%s)",
                                item.message,
                                item.error_type,
                                extra={
                                    "error": item.message,
                                    "error_type": item.error_type,
                                    "status": item.status,
                                    "oc_session_id": session_id,
                                },
                            )
                            yield format_event(
                                "error",
                                {
                                    "error": item.message,
                                    "error_type": item.error_type,
                                },
                            )
                            return

                        if isinstance(item, _BusReconnected):
                            logger.warning(
                                "chat/stream: bus reconnected mid-turn; "
                                "surfacing as error",
                                extra={"oc_session_id": session_id},
                            )
                            yield format_event(
                                "error",
                                {
                                    "error": (
                                        "opencode event bus reconnected "
                                        "mid-turn; results may be incomplete"
                                    ),
                                    "error_type": "BusReconnected",
                                },
                            )
                            return

                        if isinstance(item, dict):
                            ev = map_opencode_event(item, state)
                            if ev is not None:
                                yield format_event("thinking", ev.to_payload())
                            # Non-mapped dicts (session.idle,
                            # session.updated, step-start, etc.) are
                            # dropped silently — they have no user-
                            # visible surface at this layer.
                            continue

                        # Unknown sentinel — defensive ignore. Shouldn't
                        # happen with current bus + handler code; if it
                        # does, log once and drop.
                        logger.warning(
                            "chat/stream: dropping unknown queue item type %s",
                            type(item).__name__,
                            extra={"oc_session_id": session_id},
                        )
            except OpencodeUnavailable as exc:
                logger.warning(
                    "chat/stream: bus subscribe failed: %s",
                    exc,
                    extra={"oc_session_id": session_id},
                )
                yield format_event(
                    "error",
                    {
                        "error": str(exc),
                        "error_type": "OpencodeUnavailable",
                    },
                )
        finally:
            # Cancel the poster if still in flight (client disconnect,
            # bus reconnect, opencode-side error event, etc.). The
            # cancellation propagates into the httpx request and aborts
            # the in-flight POST; opencode may still finish the turn
            # server-side but we no longer care about its return value.
            if post_task is not None and not post_task.done():
                post_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await post_task

    return StreamingResponse(
        stream_with_keepalive(event_source()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

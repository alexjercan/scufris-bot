"""Chat endpoints — sync and SSE streaming."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from utils import ToolCallbackHandler
from utils.callbacks import ThinkingEvent
from utils.telemetry import begin_turn

from ..auth import require_token
from ..locks import (
    add_user_sink,
    current_user_id,
    get_user_lock,
    remove_user_sink,
)
from ..sse import KEEPALIVE_SECONDS, format_event, keepalive_frame

router = APIRouter(prefix="/v1", tags=["chat"])
logger = logging.getLogger("scufris-server.chat")


class ChatRequest(BaseModel):
    user_id: int = Field(...)
    message: str = Field(..., min_length=1)


class ChatResponse(BaseModel):
    user_id: int
    response: str


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


@dataclass
class _Done:
    text: str


@dataclass
class _Err:
    message: str


def _serialize_event(ev: ThinkingEvent) -> dict:
    """Convert a ThinkingEvent to the JSON payload sent over SSE."""
    payload: dict = {
        "kind": ev.kind,
        "source": ev.source,
        "text": ev.text,
        "depth": ev.depth,
    }
    if ev.arg is not None:
        payload["arg"] = ev.arg
    if ev.context is not None:
        payload["context"] = ev.context
    if ev.prior_turns is not None:
        payload["prior_turns"] = ev.prior_turns
    if ev.evicted is not None:
        payload["evicted"] = ev.evicted
    if ev.new_facts is not None:
        payload["new_facts"] = ev.new_facts
    return payload


async def _run_turn(
    request: Request,
    user_id: int,
    message: str,
    extra_callbacks: Optional[List] = None,
) -> str:
    """Push a user message through the agent, persisting history.

    Wraps the call in ``begin_turn`` (telemetry correlation) and the
    per-user lock (so two requests from the same user are serialised).
    """
    runtime = request.app.state.runtime
    history_manager = runtime.history_manager
    agent_manager = runtime.agent_manager

    lock = get_user_lock(user_id)
    async with lock:
        messages = history_manager.get_history_with_new_message(user_id, message)
        token = current_user_id.set(user_id)
        try:
            with begin_turn(f"http:{user_id}"):
                response_text = await agent_manager.process_message(
                    messages, user_id, extra_callbacks=extra_callbacks
                )
        finally:
            current_user_id.reset(token)
        history_manager.add_user_message(user_id, message)
        history_manager.add_ai_message(user_id, response_text)
        return response_text


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------


@router.post(
    "/chat",
    dependencies=[Depends(require_token)],
    response_model=ChatResponse,
)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    """Synchronous chat — returns the final reply only."""
    try:
        text = await _run_turn(request, body.user_id, body.message)
    except Exception as exc:  # noqa: BLE001
        logger.exception("chat failed for user %s", body.user_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return ChatResponse(user_id=body.user_id, response=text)


@router.post("/chat/stream", dependencies=[Depends(require_token)])
async def chat_stream(request: Request, body: ChatRequest) -> StreamingResponse:
    """Stream thinking events as SSE; finishes with a single ``done`` event.

    On error, emits a single ``error`` event and closes. Exactly one
    terminal event (``done`` or ``error``) is sent per stream.
    """

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _push(ev: ThinkingEvent) -> None:
        # Called from the agent thread (LangChain callbacks may run on
        # whichever thread the runnable was invoked on). Bounce onto
        # the event loop so the queue is touched from the right thread.
        loop.call_soon_threadsafe(queue.put_nowait, ev)

    handler = ToolCallbackHandler(on_thinking=_push)
    add_user_sink(body.user_id, _push)

    async def _runner() -> None:
        try:
            text = await _run_turn(
                request, body.user_id, body.message, extra_callbacks=[handler]
            )
            await queue.put(_Done(text))
        except Exception as exc:  # noqa: BLE001
            logger.exception("stream turn failed for user %s", body.user_id)
            await queue.put(_Err(str(exc)))

    task = asyncio.create_task(_runner())

    async def _gen() -> AsyncIterator[bytes]:
        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=KEEPALIVE_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield keepalive_frame()
                    continue
                if isinstance(item, _Done):
                    yield format_event("done", {"text": item.text})
                    return
                if isinstance(item, _Err):
                    yield format_event("error", {"error": item.message})
                    return
                if isinstance(item, ThinkingEvent):
                    yield format_event("thinking", _serialize_event(item))
                    continue
                # Unknown sentinel — ignore.
        finally:
            remove_user_sink(body.user_id, _push)
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

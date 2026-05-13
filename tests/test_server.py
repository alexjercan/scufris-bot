"""Integration tests for the Scufris HTTP server.

We bypass :func:`scufris_server.bootstrap.build_runtime` by injecting a
stub :class:`Runtime` so the tests don't need an Ollama instance, a
real LangChain agent, or any filesystem state. The HTTP layer (auth,
SSE framing, routing, per-user lock semantics) is what we're exercising.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Dict, List

import httpx
import pytest
from httpx import ASGITransport

from scufris_server.app import create_app
from scufris_server.bootstrap import Runtime

# ----------------------------------------------------------------------
# Stubs
# ----------------------------------------------------------------------


class _StubConfig:
    ollama_model = "stub-model"
    ollama_base_url = "http://stub:11434"
    max_history_per_user = 20


class _StubHistory:
    """Minimal stand-in for ChatHistoryManager."""

    def __init__(self) -> None:
        self.added_user: List[tuple] = []
        self.added_ai: List[tuple] = []

    def get_history_with_new_message(
        self, user_id: int, message: str
    ) -> List[Dict[str, str]]:
        return [{"role": "user", "content": message}]

    def add_user_message(self, user_id: int, message: str) -> None:
        self.added_user.append((user_id, message))

    def add_ai_message(self, user_id: int, message: str) -> None:
        self.added_ai.append((user_id, message))

    def get_user_telemetry(self, user_id: int) -> Dict[str, Dict[str, Any]]:
        return {}

    def get_user_breakdown(self, user_id: int) -> Dict[str, int]:
        return {"scufris": 2}

    def clear_user(self, user_id: int) -> int:
        return 2

    def get_stats(self) -> Dict[str, Any]:
        return {
            "total_users": 0,
            "total_messages": 0,
            "max_history_per_user": 20,
            "messages_per_agent": {},
            "total_invocations": 0,
        }


@dataclass
class _StubAgentManager:
    reply: str = "stub-reply"

    async def process_message(
        self,
        messages: List[Dict[str, str]],
        user_id: int,
        extra_callbacks: Any = None,
    ) -> str:
        # Simulate a tiny bit of async work so concurrent calls
        # actually overlap if scheduled in parallel.
        await asyncio.sleep(0.01)
        return self.reply


def _make_app(reply: str = "stub-reply") -> Any:
    runtime = Runtime(
        config=_StubConfig(),  # type: ignore[arg-type]
        history_manager=_StubHistory(),  # type: ignore[arg-type]
        agent_manager=_StubAgentManager(reply=reply),  # type: ignore[arg-type]
    )
    return create_app(runtime=runtime)


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    )


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


def test_healthz_no_auth() -> None:
    app = _make_app()

    async def go() -> None:
        async with _client(app) as ac:
            r = await ac.get("/v1/healthz")
            assert r.status_code == 200
            assert r.json() == {"status": "ok"}

    asyncio.run(go())


def test_chat_returns_stub_reply() -> None:
    app = _make_app(reply="hello world")

    async def go() -> None:
        async with _client(app) as ac:
            r = await ac.post("/v1/chat", json={"user_id": 42, "message": "hi"})
            assert r.status_code == 200, r.text
            assert r.json() == {"user_id": 42, "response": "hello world"}

    asyncio.run(go())


def test_chat_persists_history() -> None:
    app = _make_app(reply="ok")
    runtime: Runtime = app.state.runtime

    async def go() -> None:
        async with _client(app) as ac:
            r = await ac.post("/v1/chat", json={"user_id": 7, "message": "ping"})
            assert r.status_code == 200

    asyncio.run(go())
    assert runtime.history_manager.added_user == [(7, "ping")]  # type: ignore[attr-defined]
    assert runtime.history_manager.added_ai == [(7, "ok")]  # type: ignore[attr-defined]


def test_clear_endpoint() -> None:
    app = _make_app()

    async def go() -> None:
        async with _client(app) as ac:
            r = await ac.post("/v1/clear", json={"user_id": 1})
            assert r.status_code == 200
            assert r.json() == {
                "user_id": 1,
                "cleared": 2,
                "breakdown": {"scufris": 2},
            }

    asyncio.run(go())


def test_stats_endpoint() -> None:
    app = _make_app()

    async def go() -> None:
        async with _client(app) as ac:
            r = await ac.get("/v1/stats", params={"user_id": 1})
            assert r.status_code == 200
            body = r.json()
            assert body["user_id"] == 1
            assert isinstance(body["lines"], list)
            assert body["rows"] == {}

    asyncio.run(go())


def test_version_endpoint() -> None:
    app = _make_app()

    async def go() -> None:
        async with _client(app) as ac:
            r = await ac.get("/v1/version")
            assert r.status_code == 200
            body = r.json()
            assert body["model"] == "stub-model"

    asyncio.run(go())


def test_bearer_auth_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCUFRIS_TOKEN", "secret")
    app = _make_app()

    async def go() -> None:
        async with _client(app) as ac:
            # Healthz is unauth so still works
            assert (await ac.get("/v1/healthz")).status_code == 200
            # No token → 401
            r = await ac.post("/v1/chat", json={"user_id": 1, "message": "hi"})
            assert r.status_code == 401
            # Bad token → 401
            r = await ac.post(
                "/v1/chat",
                json={"user_id": 1, "message": "hi"},
                headers={"Authorization": "Bearer nope"},
            )
            assert r.status_code == 401
            # Correct token → 200
            r = await ac.post(
                "/v1/chat",
                json={"user_id": 1, "message": "hi"},
                headers={"Authorization": "Bearer secret"},
            )
            assert r.status_code == 200

    try:
        asyncio.run(go())
    finally:
        os.environ.pop("SCUFRIS_TOKEN", None)


def test_chat_stream_emits_done_event() -> None:
    app = _make_app(reply="streamed-reply")

    async def go() -> None:
        async with _client(app) as ac:
            async with ac.stream(
                "POST",
                "/v1/chat/stream",
                json={"user_id": 99, "message": "hello"},
            ) as r:
                assert r.status_code == 200
                assert r.headers["content-type"].startswith("text/event-stream")
                body = b""
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if b"event: done" in body:
                        break
        assert b"event: done" in body
        assert b'"text":"streamed-reply"' in body

    asyncio.run(go())

"""Integration tests for the Scufris HTTP server.

We bypass :func:`scufris_server.bootstrap.build_runtime` by injecting a
stub :class:`Runtime` so the tests don't need an Ollama instance, a
real LangChain agent, or any filesystem state. The HTTP layer (auth,
SSE framing, routing, per-user lock semantics) is what we're exercising.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import httpx
from httpx import ASGITransport

from scufris_server.app import create_app
from scufris_server.bootstrap import Runtime
from utils import Config, OllamaSection, ServerSection

# ----------------------------------------------------------------------
# Stubs
# ----------------------------------------------------------------------


def _stub_config(*, token: str | None = None) -> Config:
    """Build a real Config with the fields the routes actually touch.
    Keeping this a real Config (rather than a duck-typed shim) means
    the runtime sees the same shape production code does."""
    return Config(
        ollama=OllamaSection(model="stub-model", base_url="http://stub:11434"),
        server=ServerSection(token=token),
    )


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

    def get_tool_invocations(self, user_id: int) -> Dict[str, int]:
        return {}

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

    async def delete_session(self, user_id: int) -> None:
        # ``/v1/clear`` calls this to forget the OpenCode session
        # backing this user. The real implementation tolerates absence
        # silently; the stub mirrors that.
        return None


class _StubOpenCodeClient:
    """Minimal stand-in for :class:`utils.OpenCodeClient`.

    Only the attributes touched by the routes under test are populated:
    ``/v1/version`` reads ``base_url``, ``provider_id`` and ``model_id``.
    The lifespan-driven ``aclose()`` is bypassed in tests because
    ``create_app(runtime=...)`` skips the lifespan.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://stub-opencode:4096",
        provider_id: str = "stub-provider",
        model_id: str = "stub-opencode-model",
    ) -> None:
        self.base_url = base_url
        self.provider_id = provider_id
        self.model_id = model_id

    async def aclose(self) -> None:  # pragma: no cover — lifespan bypassed
        return None


def _make_app(
    reply: str = "stub-reply",
    *,
    config: Config | None = None,
    user_config: Any = None,
) -> Any:
    """Build a FastAPI app with a stub Runtime.

    ``user_config`` is accepted for backwards compat with the identity
    tests (which pass a config-with-bindings); when only it is set we
    use it as the full Config since the unified schema collapsed the
    two objects.
    """
    if config is None:
        config = user_config if user_config is not None else _stub_config()
    # Tests run with create_app(runtime=...) which skips the lifespan,
    # so the SessionStore here is just a placeholder satisfying the
    # dataclass — its file is never written or read by the routes
    # exercised below. Pointing at a tmp path keeps stray writes
    # (if any future route mutates it) out of <repo>/data.
    import tempfile

    from utils import SessionStore

    tmp_dir = Path(tempfile.mkdtemp(prefix="scufris-test-store-"))
    runtime = Runtime(
        config=config,
        history_manager=_StubHistory(),  # type: ignore[arg-type]
        agent_manager=_StubAgentManager(reply=reply),  # type: ignore[arg-type]
        opencode_client=_StubOpenCodeClient(),  # type: ignore[arg-type]
        session_store=SessionStore(tmp_dir / "sessions.json"),
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


def test_bearer_auth_enforced() -> None:
    # Token now lives on Config (env override is applied at load_config()
    # time, but the stub runtime bypasses that). Set it directly on the
    # config so the route's require_token dependency sees it.
    app = _make_app(config=_stub_config(token="secret"))

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

    asyncio.run(go())


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


# ----------------------------------------------------------------------
# /v1/identity/resolve
# ----------------------------------------------------------------------


def _identity_app(*, username: str = "alex", **bindings: str) -> Any:
    """Build an app whose Config has a single user with the given
    surface bindings."""
    from utils import UserIdentity, UserSection

    cfg = Config(
        user=UserSection(
            username=username,
            identity=UserIdentity(bindings=dict(bindings)),
        )
    )
    return _make_app(config=cfg)


def test_identity_resolve_matches_binding_to_username() -> None:
    from scufris_client.client import user_id_for

    app = _identity_app(telegram="42", cli="alex")

    async def go() -> None:
        async with _client(app) as ac:
            r = await ac.post(
                "/v1/identity/resolve",
                json={"surface": "telegram", "surface_id": "42"},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["user_id"] == user_id_for("alex")
            assert body["username"] == "alex"
            # Bindings should round-trip in sorted order.
            assert body["bound_surfaces"] == ["cli", "telegram"]

    asyncio.run(go())


def test_identity_resolve_unmapped_falls_back_to_int() -> None:
    # Empty config means we fall through to the numeric pass-through.
    app = _make_app()

    async def go() -> None:
        async with _client(app) as ac:
            r = await ac.post(
                "/v1/identity/resolve",
                json={"surface": "telegram", "surface_id": "8231376426"},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["user_id"] == 8231376426
            assert body["username"] is None
            assert body["bound_surfaces"] == []

    asyncio.run(go())


def test_identity_resolve_text_surface_id_falls_back_to_hash() -> None:
    from scufris_client.client import user_id_for

    app = _make_app()

    async def go() -> None:
        async with _client(app) as ac:
            r = await ac.post(
                "/v1/identity/resolve",
                json={"surface": "cli", "surface_id": "alex"},
            )
            assert r.status_code == 200
            body = r.json()
            # No mapping → username stays None even though hash matches.
            assert body["user_id"] == user_id_for("alex")
            assert body["username"] is None

    asyncio.run(go())


def test_identity_resolve_requires_auth_when_token_set() -> None:
    app = _make_app(config=_stub_config(token="secret"))

    async def go() -> None:
        async with _client(app) as ac:
            # Missing Authorization header.
            r = await ac.post(
                "/v1/identity/resolve",
                json={"surface": "cli", "surface_id": "alex"},
            )
            assert r.status_code == 401
            # With correct token works.
            r = await ac.post(
                "/v1/identity/resolve",
                headers={"Authorization": "Bearer secret"},
                json={"surface": "cli", "surface_id": "alex"},
            )
            assert r.status_code == 200

    asyncio.run(go())


def test_identity_resolve_rejects_empty_surface() -> None:
    app = _make_app()

    async def go() -> None:
        async with _client(app) as ac:
            r = await ac.post(
                "/v1/identity/resolve",
                json={"surface": "", "surface_id": "alex"},
            )
            assert r.status_code == 422  # pydantic validation

    asyncio.run(go())

"""Tests for ``scufris_server.opencode_client`` (step 4 of #9).

Uses :mod:`respx` to stub the httpx transport — no opencode process
required. Real-stack integration coverage comes later via the marker
``@pytest.mark.integration`` (step 9).
"""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx
from httpx import Response

from scufris_server.opencode_client import (
    AssistantMessage,
    HealthResponse,
    ModelRef,
    OpencodeClient,
    OpencodeClientError,
    OpencodeNetworkError,
    OpencodeServerError,
    OpencodeUnavailable,
    SendMessageRequest,
    Session,
    TextPartInput,
)

BASE = "http://opencode.test"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client() -> Any:
    """Bare client (no auth) against the fake base URL."""
    async with OpencodeClient(BASE) as c:
        yield c


@pytest_asyncio.fixture
async def authed_client() -> Any:
    """Client carrying an HTTP Basic password."""
    async with OpencodeClient(BASE, password="hunter2") as c:
        yield c


def _health_body(version: str = "1.15.13") -> dict[str, Any]:
    return {"healthy": True, "version": version}


def _session_body(session_id: str = "ses_abc") -> dict[str, Any]:
    return {"id": session_id, "slug": "test", "projectID": "prj_x"}


def _message_body(text: str = "hello") -> dict[str, Any]:
    """Shape mirrors /tmp/oc-probe/message.json — info + parts."""
    return {
        "info": {
            "id": "msg_abc",
            "sessionID": "ses_abc",
            "role": "assistant",
            "providerID": "ollama",
            "modelID": "qwen3:latest",
            "cost": 0.0,
            "tokens": {"input": 5, "output": 10},
            "finish": "stop",
        },
        "parts": [
            {"type": "step-start"},
            {"type": "reasoning", "text": "thinking..."},
            {"type": "text", "text": text},
            {"type": "step-finish", "reason": "stop"},
        ],
    }


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_happy_path(client: OpencodeClient) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/global/health").mock(
            return_value=Response(200, json=_health_body())
        )
        h = await client.health()
    assert isinstance(h, HealthResponse)
    assert h.healthy is True
    assert h.version == "1.15.13"


@pytest.mark.asyncio
async def test_health_raises_unavailable_on_connect_error(
    client: OpencodeClient,
) -> None:
    with respx.mock() as mock:
        mock.get(f"{BASE}/global/health").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with pytest.raises(OpencodeUnavailable) as exc_info:
            await client.health()
    assert "cannot reach opencode" in str(exc_info.value)


@pytest.mark.asyncio
async def test_health_raises_unavailable_on_503(
    client: OpencodeClient,
) -> None:
    with respx.mock() as mock:
        mock.get(f"{BASE}/global/health").mock(
            return_value=Response(503, text="service down")
        )
        with pytest.raises(OpencodeUnavailable) as exc_info:
            await client.health()
    assert "503" in str(exc_info.value)


@pytest.mark.asyncio
async def test_health_raises_unavailable_on_timeout(
    client: OpencodeClient,
) -> None:
    with respx.mock() as mock:
        mock.get(f"{BASE}/global/health").mock(
            side_effect=httpx.ReadTimeout("read timeout")
        )
        with pytest.raises(OpencodeUnavailable):
            await client.health()


# ---------------------------------------------------------------------------
# create_session()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_happy_no_args(
    client: OpencodeClient,
) -> None:
    with respx.mock() as mock:
        route = mock.post(f"{BASE}/session").mock(
            return_value=Response(200, json=_session_body())
        )
        s = await client.create_session()

    assert isinstance(s, Session)
    assert s.id == "ses_abc"
    # No args → empty body, not null/missing.
    sent = route.calls.last.request
    assert sent.content == b"{}"


@pytest.mark.asyncio
async def test_create_session_passes_through_kwargs(
    client: OpencodeClient,
) -> None:
    with respx.mock() as mock:
        route = mock.post(f"{BASE}/session").mock(
            return_value=Response(200, json=_session_body("ses_new"))
        )
        s = await client.create_session(
            title="My Chat", agent="scufris", parent_id="ses_parent"
        )

    assert s.id == "ses_new"
    body = route.calls.last.request.read()
    import json

    parsed = json.loads(body)
    assert parsed == {
        "title": "My Chat",
        "agent": "scufris",
        "parentID": "ses_parent",
    }


@pytest.mark.asyncio
async def test_create_session_4xx_raises_client_error(
    client: OpencodeClient,
) -> None:
    with respx.mock() as mock:
        mock.post(f"{BASE}/session").mock(
            return_value=Response(400, json={"error": "bad agent"})
        )
        with pytest.raises(OpencodeClientError) as exc_info:
            await client.create_session(agent="ghost")
    assert exc_info.value.status_code == 400
    assert exc_info.value.body == {"error": "bad agent"}


@pytest.mark.asyncio
async def test_create_session_5xx_raises_server_error(
    client: OpencodeClient,
) -> None:
    with respx.mock() as mock:
        mock.post(f"{BASE}/session").mock(return_value=Response(503))
        with pytest.raises(OpencodeServerError) as exc_info:
            await client.create_session()
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_create_session_network_error(
    client: OpencodeClient,
) -> None:
    with respx.mock() as mock:
        mock.post(f"{BASE}/session").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(OpencodeNetworkError) as exc_info:
            await client.create_session()
    assert exc_info.value.original is not None
    assert isinstance(exc_info.value.original, httpx.ConnectError)


# ---------------------------------------------------------------------------
# send_message()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_happy_path_returns_assistant_message(
    client: OpencodeClient,
) -> None:
    with respx.mock() as mock:
        route = mock.post(f"{BASE}/session/ses_abc/message").mock(
            return_value=Response(200, json=_message_body("hi!"))
        )
        req = SendMessageRequest(
            parts=[TextPartInput(text="hello")],
            model=ModelRef(providerID="ollama", modelID="qwen3:latest"),
            agent="scufris",
        )
        msg = await client.send_message("ses_abc", req)

    assert isinstance(msg, AssistantMessage)
    assert msg.info.id == "msg_abc"
    assert msg.info.providerID == "ollama"
    assert msg.text() == "hi!"
    assert len(msg.parts) == 4

    # Outbound payload omits None fields (system, tools).
    import json

    sent = json.loads(route.calls.last.request.read())
    assert "system" not in sent
    assert "tools" not in sent
    assert sent["parts"] == [{"type": "text", "text": "hello"}]
    assert sent["model"] == {"providerID": "ollama", "modelID": "qwen3:latest"}
    assert sent["agent"] == "scufris"


@pytest.mark.asyncio
async def test_send_message_text_skips_reasoning_and_step_parts(
    client: OpencodeClient,
) -> None:
    body = _message_body("ANSWER")
    # Add a second text part to confirm concatenation.
    body["parts"].append({"type": "text", "text": " more"})
    with respx.mock() as mock:
        mock.post(f"{BASE}/session/ses_x/message").mock(
            return_value=Response(200, json=body)
        )
        msg = await client.send_message(
            "ses_x",
            SendMessageRequest(parts=[TextPartInput(text="q")]),
        )
    assert msg.text() == "ANSWER more"


@pytest.mark.asyncio
async def test_send_message_503_raises_server_error(
    client: OpencodeClient,
) -> None:
    with respx.mock() as mock:
        mock.post(f"{BASE}/session/ses_x/message").mock(
            return_value=Response(503, text="overloaded")
        )
        with pytest.raises(OpencodeServerError) as exc_info:
            await client.send_message(
                "ses_x",
                SendMessageRequest(parts=[TextPartInput(text="q")]),
            )
    assert exc_info.value.status_code == 503
    assert exc_info.value.body == "overloaded"  # not JSON → text fallback


@pytest.mark.asyncio
async def test_send_message_404_raises_client_error(
    client: OpencodeClient,
) -> None:
    with respx.mock() as mock:
        mock.post(f"{BASE}/session/ses_missing/message").mock(
            return_value=Response(404, json={"error": "no such session"})
        )
        with pytest.raises(OpencodeClientError) as exc_info:
            await client.send_message(
                "ses_missing",
                SendMessageRequest(parts=[TextPartInput(text="q")]),
            )
    assert exc_info.value.status_code == 404
    assert exc_info.value.body == {"error": "no such session"}


@pytest.mark.asyncio
async def test_send_message_network_error(client: OpencodeClient) -> None:
    with respx.mock() as mock:
        mock.post(f"{BASE}/session/ses_x/message").mock(
            side_effect=httpx.ConnectError("refused")
        )
        with pytest.raises(OpencodeNetworkError):
            await client.send_message(
                "ses_x",
                SendMessageRequest(parts=[TextPartInput(text="q")]),
            )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_basic_auth_header_sent_when_password_set(
    authed_client: OpencodeClient,
) -> None:
    with respx.mock() as mock:
        route = mock.get(f"{BASE}/global/health").mock(
            return_value=Response(200, json=_health_body())
        )
        await authed_client.health()

    auth_header = route.calls.last.request.headers.get("authorization")
    assert auth_header is not None
    assert auth_header.startswith("Basic ")
    decoded = base64.b64decode(auth_header.removeprefix("Basic ")).decode()
    # Username is empty per opencode contract.
    assert decoded == ":hunter2"


@pytest.mark.asyncio
async def test_no_auth_header_when_password_unset(
    client: OpencodeClient,
) -> None:
    with respx.mock() as mock:
        route = mock.get(f"{BASE}/global/health").mock(
            return_value=Response(200, json=_health_body())
        )
        await client.health()
    assert route.calls.last.request.headers.get("authorization") is None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    c = OpencodeClient(BASE)
    await c.close()
    await c.close()  # must not raise


@pytest.mark.asyncio
async def test_async_context_manager_closes_client() -> None:
    async with OpencodeClient(BASE) as c:
        assert not c._client.is_closed
    assert c._client.is_closed


@pytest.mark.asyncio
async def test_base_url_trailing_slash_stripped() -> None:
    async with OpencodeClient(f"{BASE}/") as c:
        with respx.mock() as mock:
            mock.get(f"{BASE}/global/health").mock(
                return_value=Response(200, json=_health_body())
            )
            await c.health()

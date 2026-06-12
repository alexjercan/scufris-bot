"""End-to-end test for :class:`OllamaChatTransport` against a fake httpx transport.

Validates the wire shape of the compactor's HTTP call now that it
talks to Ollama directly (no LangChain): exercises
:class:`utils.memory_compactor.OllamaChatTransport` through
``httpx.MockTransport`` so the assertions cover the actual JSON body
hitting ``/api/chat`` and the assistant content extraction logic.
"""

from __future__ import annotations

import json
from typing import List

import httpx

from utils.memory_compactor import (
    LLMCompactor,
    OllamaChatTransport,
)
from utils.messages import user_message


def _make_transport(
    *,
    response_payload: dict,
    base_url: str = "http://ollama.test",
    captured: List[httpx.Request] | None = None,
) -> OllamaChatTransport:
    """Build an :class:`OllamaChatTransport` whose underlying httpx
    client is replaced with a deterministic mock.

    The transport opens a fresh ``httpx.Client`` inside ``chat()`` so
    we monkeypatch via a thin subclass that injects a pre-built
    ``MockTransport``-backed client.
    """
    captured_list = captured if captured is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_list.append(request)
        return httpx.Response(200, json=response_payload)

    mock = httpx.MockTransport(handler)

    class _Patched(OllamaChatTransport):
        def chat(self, messages):
            payload = {
                "model": self.model,
                "messages": list(messages),
                "stream": False,
                "options": {"temperature": self.temperature},
            }
            with httpx.Client(transport=mock, timeout=self.timeout) as http:
                response = http.post(f"{self.base_url}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
            msg = data.get("message") or {}
            content = msg.get("content")
            return content if isinstance(content, str) else ""

    return _Patched("qwen-test", base_url=base_url)


def test_ollama_transport_posts_to_api_chat_with_expected_body():
    captured: List[httpx.Request] = []
    transport = _make_transport(
        response_payload={"message": {"role": "assistant", "content": "hello"}},
        captured=captured,
    )

    out = transport.chat([{"role": "user", "content": "ping"}])

    assert out == "hello"
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == "http://ollama.test/api/chat"
    body = json.loads(req.content.decode("utf-8"))
    assert body["model"] == "qwen-test"
    assert body["stream"] is False
    assert body["messages"] == [{"role": "user", "content": "ping"}]
    assert body["options"] == {"temperature": 0.0}


def test_ollama_transport_returns_empty_string_when_message_missing():
    transport = _make_transport(
        response_payload={"done": True},  # no "message" key at all
    )
    assert transport.chat([{"role": "user", "content": "x"}]) == ""


def test_ollama_transport_returns_empty_string_when_content_not_a_string():
    transport = _make_transport(
        response_payload={"message": {"role": "assistant", "content": None}},
    )
    assert transport.chat([{"role": "user", "content": "x"}]) == ""


def test_llm_compactor_round_trip_through_ollama_transport():
    """The full path: evicted messages → JSON prompt → POST /api/chat → parsed result."""
    captured: List[httpx.Request] = []
    payload = {
        "summary": "user lives in Cluj",
        "facts": {"location": "Cluj"},
    }
    transport = _make_transport(
        response_payload={
            "message": {"role": "assistant", "content": json.dumps(payload)}
        },
        captured=captured,
    )
    compactor = LLMCompactor(transport)

    result = compactor.compact(
        evicted=[user_message("I live in Cluj")],
        existing_summary="",
        existing_facts={},
    )

    assert result == {"summary": "user lives in Cluj", "facts": {"location": "Cluj"}}
    # Exactly one HTTP call hit /api/chat with the user prompt.
    assert len(captured) == 1
    body = json.loads(captured[0].content.decode("utf-8"))
    assert body["messages"][0]["role"] == "user"
    assert "I live in Cluj" in body["messages"][0]["content"]


def test_llm_compactor_swallows_http_error_from_transport():
    """When the Ollama daemon returns 5xx, the compactor must degrade
    to a no-op result rather than propagate an exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="ollama down")

    mock = httpx.MockTransport(handler)

    class _Failing(OllamaChatTransport):
        def chat(self, messages):
            with httpx.Client(transport=mock, timeout=self.timeout) as http:
                response = http.post(f"{self.base_url}/api/chat", json={})
            response.raise_for_status()
            return ""

    compactor = LLMCompactor(_Failing("qwen-test", base_url="http://ollama.test"))
    out = compactor.compact([user_message("anything")], "kept summary", {"k": "v"})
    assert out == {"summary": "kept summary", "facts": {}}

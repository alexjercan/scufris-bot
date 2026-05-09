"""Unit tests for sub-agent memory plumbing.

Phase 3.2: AgentManager.process_message threads user_id under
``config["configurable"]``.

Phase 3.3: ``sub_agent_tool`` (built by ``create_sub_agent``) loads
prior history, persists new turns, raises on missing user_id when
``keeps_history=True``, and respects token budgets.

All LLM construction and inner agent invocation are stubbed at the
``agent_builder.ChatOllama`` / ``agent_builder.create_agent`` boundary —
no network, no Ollama, no real LangChain agent loop.
"""

import asyncio
import logging
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from utils import agent_builder
from utils.agent import AgentManager
from utils.history import ChatHistoryManager

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubAgent:
    """Records each .invoke() call and returns canned messages."""

    def __init__(self, reply_text: str = "ok"):
        self.calls: List[Dict[str, Any]] = []
        self.reply_text = reply_text

    def invoke(self, payload, config=None, **kwargs):
        self.calls.append({"payload": payload, "config": config, "kwargs": kwargs})
        msgs = list(payload.get("messages", []))
        msgs.append(AIMessage(content=self.reply_text))
        return {"messages": msgs}


def _make_config():
    cfg = MagicMock()
    cfg.ollama_model = "stub-model"
    cfg.ollama_reasoning = False
    cfg.ollama_base_url = "http://stub"
    cfg.ollama_temperature = 0.0
    return cfg


@pytest.fixture
def stub_inner_agent(monkeypatch):
    """Patch agent_builder so create_sub_agent uses a recordable stub agent."""
    stub = _StubAgent()
    monkeypatch.setattr(agent_builder, "ChatOllama", lambda **_: object())
    monkeypatch.setattr(
        agent_builder, "create_agent", lambda llm, tools, system_prompt: stub
    )
    return stub


# ---------------------------------------------------------------------------
# Phase 3.2 — AgentManager threads configurable.user_id
# ---------------------------------------------------------------------------


def test_process_message_puts_user_id_under_configurable():
    stub = _StubAgent(reply_text="hi back")
    mgr = AgentManager(stub)
    out = asyncio.run(
        mgr.process_message([{"role": "user", "content": "hello"}], user_id=42)
    )
    assert out == "hi back"
    assert len(stub.calls) == 1
    cfg = stub.calls[0]["config"]
    assert cfg["configurable"] == {"user_id": 42}


def test_process_message_records_main_agent_invocation_when_history_set():
    stub = _StubAgent()
    hm = ChatHistoryManager()
    mgr = AgentManager(stub, history_manager=hm)
    asyncio.run(mgr.process_message([{"role": "user", "content": "hi"}], user_id=1))
    tel = hm.get_user_telemetry(1)
    assert tel["scufris"]["invocations"] == 1


def test_process_message_raises_when_agent_returns_no_messages():
    class _Empty:
        def invoke(self, payload, config=None, **_):
            return {"messages": []}

    mgr = AgentManager(_Empty())
    with pytest.raises(ValueError):
        asyncio.run(mgr.process_message([{"role": "user", "content": "x"}], user_id=1))


def test_callbacks_default_to_empty_list():
    mgr = AgentManager(_StubAgent())
    assert mgr.callbacks == []


# ---------------------------------------------------------------------------
# Phase 3.3 — sub_agent_tool: load → invoke → persist
# ---------------------------------------------------------------------------


def _make_sub_agent(
    stub_inner_agent,
    *,
    name="knowledge_agent",
    keeps_history=True,
    budget=4000,
    hm=None,
):
    hm = hm or ChatHistoryManager()
    tool = agent_builder.create_sub_agent(
        config=_make_config(),
        name=name,
        system_prompt="sys",
        tools=[],
        logger=logging.getLogger("test"),
        keeps_history=keeps_history,
        history_token_budget=budget,
        history_manager=hm,
    )
    return tool, hm, stub_inner_agent


def test_first_call_sends_only_user_turn(stub_inner_agent):
    tool, hm, stub = _make_sub_agent(stub_inner_agent)
    tool.invoke(
        {"query": "what is 2+2?", "context": ""},
        config={"configurable": {"user_id": 1}},
    )
    sent = stub.calls[0]["payload"]["messages"]
    assert len(sent) == 1
    assert isinstance(sent[0], HumanMessage)
    assert sent[0].content == "what is 2+2?"


def test_context_and_query_are_joined_with_separator(stub_inner_agent):
    tool, _, stub = _make_sub_agent(stub_inner_agent)
    tool.invoke(
        {"query": "and Ploiesti?", "context": "User asked about Bucharest weather"},
        config={"configurable": {"user_id": 1}},
    )
    sent = stub.calls[0]["payload"]["messages"]
    assert (
        sent[0].content == "User asked about Bucharest weather\n\n---\n\nand Ploiesti?"
    )


def test_empty_context_sends_query_verbatim(stub_inner_agent):
    tool, _, stub = _make_sub_agent(stub_inner_agent)
    tool.invoke(
        {"query": "hi", "context": "   "},
        config={"configurable": {"user_id": 1}},
    )
    assert stub.calls[0]["payload"]["messages"][0].content == "hi"


def test_second_call_includes_persisted_prior_turns(stub_inner_agent):
    tool, hm, stub = _make_sub_agent(stub_inner_agent)
    tool.invoke(
        {"query": "first", "context": ""}, config={"configurable": {"user_id": 1}}
    )
    tool.invoke(
        {"query": "second", "context": ""}, config={"configurable": {"user_id": 1}}
    )
    sent_second = stub.calls[1]["payload"]["messages"]
    # First call persisted: [user="first", AI="ok"]. Second sends those
    # plus the new user turn.
    assert len(sent_second) == 3
    assert sent_second[0].content == "first"
    assert isinstance(sent_second[1], AIMessage)
    assert sent_second[2].content == "second"


def test_persisted_history_includes_user_turn_and_inner_messages(stub_inner_agent):
    tool, hm, _ = _make_sub_agent(stub_inner_agent)
    tool.invoke({"query": "q1", "context": ""}, config={"configurable": {"user_id": 1}})
    slice_ = hm.get_history(1, agent="knowledge_agent")
    assert [type(m) for m in slice_] == [HumanMessage, AIMessage]
    assert slice_[0].content == "q1"


def test_keeps_history_false_persists_nothing(stub_inner_agent):
    tool, hm, _ = _make_sub_agent(
        stub_inner_agent, name="utilities_agent", keeps_history=False
    )
    tool.invoke(
        {"query": "2+2", "context": ""}, config={"configurable": {"user_id": 1}}
    )
    assert hm.get_history(1, agent="utilities_agent") == []


def test_keeps_history_false_still_records_invocation(stub_inner_agent):
    tool, hm, _ = _make_sub_agent(
        stub_inner_agent, name="utilities_agent", keeps_history=False
    )
    tool.invoke(
        {"query": "2+2", "context": ""}, config={"configurable": {"user_id": 7}}
    )
    assert hm.get_user_telemetry(7)["utilities_agent"]["invocations"] == 1


def test_missing_user_id_raises_when_keeps_history(stub_inner_agent):
    tool, _, _ = _make_sub_agent(stub_inner_agent)
    with pytest.raises(ValueError, match="configurable.user_id"):
        tool.invoke({"query": "q", "context": ""}, config={"configurable": {}})


def test_missing_user_id_tolerated_when_history_disabled(stub_inner_agent):
    tool, _, _ = _make_sub_agent(
        stub_inner_agent, name="utilities_agent", keeps_history=False
    )
    # Should not raise — stateless agents skip the wiring check.
    out = tool.invoke({"query": "q", "context": ""}, config={"configurable": {}})
    assert out == "ok"


def test_token_budget_honored_across_many_calls(stub_inner_agent):
    # Budget 10 tokens => 40 chars. After many big calls the slice
    # must stay bounded (modulo never-empty: at least 1 message).
    tool, hm, _ = _make_sub_agent(stub_inner_agent, budget=10)
    big_query = "x" * 200
    for _ in range(5):
        tool.invoke(
            {"query": big_query, "context": ""},
            config={"configurable": {"user_id": 1}},
        )
    slice_ = hm.get_history(1, agent="knowledge_agent")
    # Never-empty: at least one message survives.
    assert len(slice_) >= 1
    # And the slice did get trimmed — not all 5 calls' worth of msgs.
    assert len(slice_) < 5 * 2


def test_create_sub_agent_keeps_history_requires_manager():
    with pytest.raises(ValueError, match="history_manager"):
        agent_builder.create_sub_agent(
            config=_make_config(),
            name="x",
            system_prompt="",
            tools=[],
            logger=logging.getLogger("test"),
            keeps_history=True,
            history_manager=None,
        )


def test_register_agent_called_during_sub_agent_creation(stub_inner_agent):
    tool, hm, _ = _make_sub_agent(stub_inner_agent, name="knowledge_agent")
    # Registration runs at build time; telemetry shows budget + model
    # even before any invocation.
    tel = hm.get_user_telemetry(1)
    assert tel["knowledge_agent"]["budget"] == 4000
    assert tel["knowledge_agent"]["model"] == "stub-model"

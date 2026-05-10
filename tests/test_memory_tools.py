"""Phase 3 tests for `remember` / `forget` tools and surrounding wiring.

Covers:
  * happy-path remember/forget against a real :class:`ChatHistoryManager`
  * input-validation errors return readable strings (don't raise)
  * tools route to the captured ``(user_id, agent_name)`` slice
  * sub-agent toolset injection (history-keeping agents only)
  * compaction event sink emits :class:`ThinkingEvent` with new fields
  * `/stats` includes the new ``summary_chars`` + ``facts_count`` columns

All tests stub at the SDK boundary (no Ollama, no LangChain agent loop).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from utils import agent_builder
from utils.callbacks import ThinkingEvent
from utils.history import ChatHistoryManager
from utils.stats import format_stats_lines
from utils.tools.memory_tools import make_memory_tools

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(user_id: int = 1) -> dict:
    return {"configurable": {"user_id": user_id}}


def _make_agent_config():
    cfg = MagicMock()
    cfg.ollama_model = "stub-model"
    cfg.ollama_reasoning = False
    cfg.ollama_base_url = "http://stub"
    cfg.ollama_temperature = 0.0
    return cfg


# ---------------------------------------------------------------------------
# remember / forget — happy path
# ---------------------------------------------------------------------------


def test_remember_writes_fact_to_correct_slice():
    hm = ChatHistoryManager()
    remember, _ = make_memory_tools(hm, "knowledge_agent")
    out = remember.invoke({"key": "location", "value": "Bucharest"}, config=_cfg(7))
    assert out == "remembered: location = Bucharest"
    assert hm.get_facts(7, "knowledge_agent") == {"location": "Bucharest"}
    # Other slices untouched.
    assert hm.get_facts(7, "coding_agent") == {}
    assert hm.get_facts(8, "knowledge_agent") == {}


def test_remember_marks_source_as_remember():
    hm = ChatHistoryManager()
    remember, _ = make_memory_tools(hm, "knowledge_agent")
    remember.invoke({"key": "tz", "value": "EET"}, config=_cfg(1))
    meta = hm.get_facts_with_meta(1, "knowledge_agent")
    assert meta["tz"].source == "remember"
    assert meta["tz"].value == "EET"


def test_forget_drops_existing_fact():
    hm = ChatHistoryManager()
    hm.add_facts(1, "journal_agent", {"goal": "eat better"})
    _, forget = make_memory_tools(hm, "journal_agent")
    out = forget.invoke({"key": "goal"}, config=_cfg(1))
    assert out == "forgot: goal"
    assert hm.get_facts(1, "journal_agent") == {}


def test_forget_returns_no_such_fact_for_missing_key():
    hm = ChatHistoryManager()
    _, forget = make_memory_tools(hm, "knowledge_agent")
    out = forget.invoke({"key": "nope"}, config=_cfg(1))
    assert out == "no such fact: nope"


# ---------------------------------------------------------------------------
# Validation — returns readable error strings, never raises
# ---------------------------------------------------------------------------


def test_remember_rejects_empty_key():
    hm = ChatHistoryManager()
    remember, _ = make_memory_tools(hm, "knowledge_agent")
    out = remember.invoke({"key": "  ", "value": "x"}, config=_cfg(1))
    assert out.startswith("error:") and "key" in out


def test_remember_rejects_oversized_key():
    hm = ChatHistoryManager()
    remember, _ = make_memory_tools(hm, "knowledge_agent")
    out = remember.invoke({"key": "k" * 50, "value": "x"}, config=_cfg(1))
    assert "key too long" in out


def test_remember_rejects_empty_value():
    hm = ChatHistoryManager()
    remember, _ = make_memory_tools(hm, "knowledge_agent")
    out = remember.invoke({"key": "k", "value": ""}, config=_cfg(1))
    assert out.startswith("error:") and "value" in out


def test_remember_rejects_oversized_value():
    hm = ChatHistoryManager()
    remember, _ = make_memory_tools(hm, "knowledge_agent")
    out = remember.invoke({"key": "k", "value": "x" * 500}, config=_cfg(1))
    assert "value too long" in out


def test_remember_returns_error_when_user_id_missing():
    hm = ChatHistoryManager()
    remember, _ = make_memory_tools(hm, "knowledge_agent")
    out = remember.invoke({"key": "k", "value": "v"}, config={"configurable": {}})
    assert "user_id missing" in out
    assert hm.get_facts(0, "knowledge_agent") == {}


def test_forget_returns_error_when_user_id_missing():
    hm = ChatHistoryManager()
    _, forget = make_memory_tools(hm, "knowledge_agent")
    out = forget.invoke({"key": "k"}, config={"configurable": {}})
    assert "user_id missing" in out


# ---------------------------------------------------------------------------
# Routing — tools always write to *their* captured slice
# ---------------------------------------------------------------------------


def test_two_factories_route_to_independent_slices():
    hm = ChatHistoryManager()
    k_remember, _ = make_memory_tools(hm, "knowledge_agent")
    j_remember, _ = make_memory_tools(hm, "journal_agent")
    k_remember.invoke({"key": "k", "value": "from-knowledge"}, config=_cfg(1))
    j_remember.invoke({"key": "k", "value": "from-journal"}, config=_cfg(1))
    assert hm.get_facts(1, "knowledge_agent") == {"k": "from-knowledge"}
    assert hm.get_facts(1, "journal_agent") == {"k": "from-journal"}


# ---------------------------------------------------------------------------
# Agent-builder injection — history-keeping agents only
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_tools(monkeypatch):
    """Patch create_agent so we can inspect the tools it received."""
    captured: dict = {}

    def fake_create_agent(llm, tools, system_prompt):
        captured["tools"] = list(tools)
        return MagicMock()

    monkeypatch.setattr(agent_builder, "ChatOllama", lambda **_: object())
    monkeypatch.setattr(agent_builder, "create_agent", fake_create_agent)
    return captured


def test_history_keeping_sub_agent_gets_remember_and_forget(captured_tools):
    hm = ChatHistoryManager()
    agent_builder.create_sub_agent(
        config=_make_agent_config(),
        name="knowledge_agent",
        system_prompt="sys",
        tools=[],
        logger=logging.getLogger("test"),
        keeps_history=True,
        history_manager=hm,
    )
    names = {getattr(t, "name", None) for t in captured_tools["tools"]}
    assert {"remember", "forget"}.issubset(names)


def test_stateless_sub_agent_does_not_get_memory_tools(captured_tools):
    hm = ChatHistoryManager()
    agent_builder.create_sub_agent(
        config=_make_agent_config(),
        name="utilities_agent",
        system_prompt="sys",
        tools=[],
        logger=logging.getLogger("test"),
        keeps_history=False,
        history_manager=hm,
    )
    names = {getattr(t, "name", None) for t in captured_tools["tools"]}
    assert "remember" not in names and "forget" not in names


# ---------------------------------------------------------------------------
# Compaction event sink — emits ThinkingEvent on non-empty salvage
# ---------------------------------------------------------------------------


class _StubCompactor:
    def __init__(self, summary: str = "stub-summary", facts=None):
        self.summary = summary
        self.facts = facts or {"x": "1"}

    def compact(self, evicted, existing_summary, existing_facts):
        return {"summary": self.summary, "facts": dict(self.facts)}


class _SilentCompactor:
    def compact(self, *_args, **_kwargs):
        return {"summary": "", "facts": {}}


def test_compaction_emits_event_with_evicted_and_new_facts_counts():
    events: List[ThinkingEvent] = []
    hm = ChatHistoryManager(
        max_history_per_user=2,
        compactor=_StubCompactor(facts={"a": "1", "b": "2"}),
    )
    hm.set_event_sink(events.append)
    # Force eviction: 3 messages over the cap of 2.
    for i in range(3):
        hm.add_user_message(1, f"m{i}")
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "compaction"
    assert ev.source == "scufris"
    assert ev.evicted == 1
    assert ev.new_facts == 2


def test_empty_compaction_does_not_emit_event():
    events: List[ThinkingEvent] = []
    hm = ChatHistoryManager(max_history_per_user=2, compactor=_SilentCompactor())
    hm.set_event_sink(events.append)
    for i in range(3):
        hm.add_user_message(1, f"m{i}")
    assert events == []


def test_event_sink_exception_does_not_break_eviction():
    def bad_sink(_ev):
        raise RuntimeError("boom")

    hm = ChatHistoryManager(max_history_per_user=2, compactor=_StubCompactor())
    hm.set_event_sink(bad_sink)
    for i in range(3):
        hm.add_user_message(1, f"m{i}")
    # Eviction still happened: only 2 messages kept.
    assert len(hm.get_history(1)) == 2


# ---------------------------------------------------------------------------
# /stats — new columns
# ---------------------------------------------------------------------------


def test_get_user_telemetry_includes_summary_chars_and_facts_count():
    hm = ChatHistoryManager()
    hm.register_agent("knowledge_agent", token_budget=4000)
    hm.add_facts(1, "knowledge_agent", {"a": "1", "b": "2"})
    hm._summaries[(1, "knowledge_agent")] = "abc"  # type: ignore[index]
    tel = hm.get_user_telemetry(1)
    assert tel["knowledge_agent"]["summary_chars"] == 3
    assert tel["knowledge_agent"]["facts_count"] == 2


def test_format_stats_lines_renders_summary_and_facts_columns():
    hm = ChatHistoryManager()
    hm.register_agent("knowledge_agent", token_budget=4000)
    hm.add_facts(1, "knowledge_agent", {"a": "1", "b": "2"})
    hm._summaries[(1, "knowledge_agent")] = "x" * 12  # type: ignore[index]
    started = datetime.now(timezone.utc) - timedelta(minutes=5)
    lines = format_stats_lines(hm, 1, started, "qwen3", "http://stub")
    header = next(line for line in lines if line.lstrip().startswith("agent"))
    assert "summary" in header and "facts" in header
    row = next(line for line in lines if "knowledge_agent" in line)
    assert "12ch" in row
    assert " 2" in row  # facts count


def test_stats_summary_facts_columns_show_dash_for_history_disabled():
    hm = ChatHistoryManager()
    hm.register_agent("utilities_agent", history_disabled=True)
    started = datetime.now(timezone.utc) - timedelta(minutes=5)
    lines = format_stats_lines(hm, 1, started, "qwen3", "http://stub")
    row = next(line for line in lines if "utilities_agent" in line)
    # Two em-dashes — one for summary col, one for facts col.
    assert row.count("—") >= 2


# ---------------------------------------------------------------------------
# Stub for HumanMessage import linter (used implicitly via history slices)
# ---------------------------------------------------------------------------

_ = HumanMessage, AIMessage

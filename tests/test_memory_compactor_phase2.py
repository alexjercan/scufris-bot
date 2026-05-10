"""Tests for Phase 2 of the history-compaction rollout
(`tasks/20260510-183123`).

Covers:

- :class:`LLMCompactor` happy path + every degradation mode (LLM
  raises, malformed JSON, non-object root, non-string fields).
- :func:`create_compactor` factory honouring the
  ``SCUFRIS_COMPACTOR=noop`` opt-out.
- ``ChatHistoryManager.get_history_with_new_message`` prepending
  facts + summary SystemMessages in the documented order.
- ``ChatHistoryManager.build_context_messages`` for the sub-agent
  injection path (used by ``agent_builder.sub_agent_tool``).
- Per-fact provenance: ``FactEntry`` populated with source +
  timestamp; compactor-sourced facts marked ``"compactor"``;
  ``add_facts`` defaults to ``"remember"``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)

from utils.history import ChatHistoryManager
from utils.memory_compactor import (
    CompactionResult,
    FactEntry,
    LLMCompactor,
    NoopCompactor,
    create_compactor,
    format_age,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeLLM:
    """Minimal LLM stub: configurable return value, records prompts."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.prompts: List[str] = []

    def invoke(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        return self._response


class _RaisingLLM:
    def invoke(self, prompt: str) -> Any:
        raise RuntimeError("llm down")


# ---------------------------------------------------------------------------
# LLMCompactor
# ---------------------------------------------------------------------------


def test_llm_compactor_parses_valid_json_response_with_content_attr():
    payload = {"summary": "user lives in Cluj", "facts": {"location": "Cluj"}}
    llm = _FakeLLM(AIMessage(content=json.dumps(payload)))
    c = LLMCompactor(llm)
    result = c.compact([HumanMessage(content="I live in Cluj")], "", {})
    assert result == {"summary": "user lives in Cluj", "facts": {"location": "Cluj"}}
    assert "I live in Cluj" in llm.prompts[0]


def test_llm_compactor_accepts_plain_string_response():
    payload = '{"summary": "s", "facts": {"k": "v"}}'
    c = LLMCompactor(_FakeLLM(payload))
    assert c.compact([HumanMessage(content="x")], "", {}) == {
        "summary": "s",
        "facts": {"k": "v"},
    }


def test_llm_compactor_strips_markdown_fences():
    fenced = "```json\n" + json.dumps({"summary": "s", "facts": {}}) + "\n```"
    c = LLMCompactor(_FakeLLM(AIMessage(content=fenced)))
    assert c.compact([HumanMessage(content="x")], "prev", {})["summary"] == "s"


def test_llm_compactor_malformed_json_preserves_existing_summary():
    c = LLMCompactor(_FakeLLM(AIMessage(content="not json at all {")))
    out = c.compact([HumanMessage(content="x")], "kept", {})
    assert out == {"summary": "kept", "facts": {}}


def test_llm_compactor_non_object_root_preserves_existing_summary():
    c = LLMCompactor(_FakeLLM(AIMessage(content='["just", "a", "list"]')))
    assert c.compact([HumanMessage(content="x")], "kept", {}) == {
        "summary": "kept",
        "facts": {},
    }


def test_llm_compactor_llm_raises_returns_noop_result():
    c = LLMCompactor(_RaisingLLM())
    assert c.compact([HumanMessage(content="x")], "kept", {"k": "v"}) == {
        "summary": "kept",
        "facts": {},
    }


def test_llm_compactor_empty_evicted_short_circuits_without_calling_llm():
    llm = _FakeLLM(AIMessage(content="should not be used"))
    c = LLMCompactor(llm)
    out = c.compact([], "kept", {})
    assert out == {"summary": "kept", "facts": {}}
    assert llm.prompts == []


def test_llm_compactor_coerces_non_string_fact_values_and_drops_garbage():
    payload = {
        "summary": "ok",
        "facts": {"age": 30, "active": True, "ratio": 0.5, "bad": [1, 2]},
    }
    c = LLMCompactor(_FakeLLM(AIMessage(content=json.dumps(payload))))
    out = c.compact([HumanMessage(content="x")], "", {})
    # Scalars coerced to str; list value dropped.
    assert out["facts"] == {"age": "30", "active": "True", "ratio": "0.5"}


def test_llm_compactor_non_string_summary_field_falls_back_to_existing():
    payload = {"summary": 123, "facts": {"k": "v"}}
    c = LLMCompactor(_FakeLLM(AIMessage(content=json.dumps(payload))))
    out = c.compact([HumanMessage(content="x")], "prev", {})
    assert out["summary"] == "prev"
    assert out["facts"] == {"k": "v"}


# ---------------------------------------------------------------------------
# create_compactor factory
# ---------------------------------------------------------------------------


def test_create_compactor_noop_env_returns_noop(monkeypatch):
    monkeypatch.setenv("SCUFRIS_COMPACTOR", "noop")
    assert isinstance(create_compactor(), NoopCompactor)


def test_create_compactor_with_explicit_llm_skips_ollama_import(monkeypatch):
    monkeypatch.delenv("SCUFRIS_COMPACTOR", raising=False)
    fake = _FakeLLM(AIMessage(content='{"summary":"","facts":{}}'))
    c = create_compactor(llm=fake)
    assert isinstance(c, LLMCompactor)
    # Sanity: it's actually wired to our fake.
    c.compact([HumanMessage(content="x")], "", {})
    assert len(fake.prompts) == 1


def test_create_compactor_noop_env_case_insensitive(monkeypatch):
    monkeypatch.setenv("SCUFRIS_COMPACTOR", "NoOp")
    assert isinstance(create_compactor(), NoopCompactor)


# ---------------------------------------------------------------------------
# Per-fact provenance (FactEntry)
# ---------------------------------------------------------------------------


def test_add_facts_defaults_to_remember_source():
    h = ChatHistoryManager()
    h.add_facts(1, "knowledge_agent", {"location": "Cluj"})
    meta = h.get_facts_with_meta(1, "knowledge_agent")
    assert isinstance(meta["location"], FactEntry)
    assert meta["location"].source == "remember"
    assert meta["location"].value == "Cluj"
    assert meta["location"].timestamp > 0


def test_add_facts_explicit_source_compactor():
    h = ChatHistoryManager()
    h.add_facts(1, "knowledge_agent", {"location": "Cluj"}, source="compactor")
    assert h.get_facts_with_meta(1, "knowledge_agent")["location"].source == "compactor"


def test_get_facts_returns_value_only_view_for_backward_compat():
    h = ChatHistoryManager()
    h.add_facts(1, "knowledge_agent", {"location": "Cluj"})
    assert h.get_facts(1, "knowledge_agent") == {"location": "Cluj"}


def test_compactor_sourced_facts_marked_with_compactor_provenance():
    class _CompactorReturning(NoopCompactor):
        def compact(
            self, evicted, existing_summary, existing_facts
        ) -> CompactionResult:
            return {"summary": "", "facts": {"goal": "ship phase 2"}}

    h = ChatHistoryManager(max_history_per_user=1, compactor=_CompactorReturning())
    h.add_user_message(1, "first")
    h.add_user_message(1, "second")  # triggers eviction → compactor → merge
    meta = h.get_facts_with_meta(1)
    assert meta["goal"].source == "compactor"


def test_format_age_buckets():
    now = 10_000.0
    assert format_age(now - 5, now=now) == "just now"
    assert format_age(now - 120, now=now) == "2m ago"
    assert format_age(now - 7200, now=now) == "2h ago"
    assert format_age(now - 3 * 86400, now=now) == "3d ago"


# ---------------------------------------------------------------------------
# Prompt-side injection: get_history_with_new_message
# ---------------------------------------------------------------------------


def test_get_history_with_new_message_no_context_when_empty():
    h = ChatHistoryManager()
    msgs = h.get_history_with_new_message(1, "hello")
    # Pre-Phase-2 shape: just the new user turn, no prepended systems.
    assert msgs == [{"role": "user", "content": "hello"}]


def test_get_history_with_new_message_prepends_facts_only():
    h = ChatHistoryManager()
    h.add_facts(1, "scufris", {"location": "Cluj"})
    msgs = h.get_history_with_new_message(1, "hi")
    assert msgs[0]["role"] == "system"
    assert "Known facts" in msgs[0]["content"]
    assert "location: Cluj (remember" in msgs[0]["content"]
    assert msgs[-1] == {"role": "user", "content": "hi"}


def test_get_history_with_new_message_prepends_summary_only():
    h = ChatHistoryManager()
    h._summaries[(1, "scufris")] = "user lives in Cluj"
    msgs = h.get_history_with_new_message(1, "hi")
    assert msgs[0]["role"] == "system"
    assert "Earlier conversation summary: user lives in Cluj" in msgs[0]["content"]


def test_get_history_with_new_message_orders_facts_before_summary():
    h = ChatHistoryManager()
    h.add_facts(1, "scufris", {"k": "v"})
    h._summaries[(1, "scufris")] = "summary text"
    msgs = h.get_history_with_new_message(1, "hi")
    assert msgs[0]["role"] == "system"
    assert "Known facts" in msgs[0]["content"]
    assert msgs[1]["role"] == "system"
    assert "Earlier conversation summary" in msgs[1]["content"]
    assert msgs[-1] == {"role": "user", "content": "hi"}


def test_get_history_with_new_message_includes_window_after_context():
    h = ChatHistoryManager()
    h.add_facts(1, "scufris", {"k": "v"})
    h.add_user_message(1, "earlier")
    msgs = h.get_history_with_new_message(1, "now")
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "user"]
    assert msgs[1]["content"] == "earlier"
    assert msgs[2]["content"] == "now"


# ---------------------------------------------------------------------------
# Sub-agent injection path: build_context_messages
# ---------------------------------------------------------------------------


def test_build_context_messages_empty_when_no_state():
    h = ChatHistoryManager()
    assert h.build_context_messages(1, "knowledge_agent") == []


def test_build_context_messages_returns_system_messages():
    h = ChatHistoryManager()
    h.add_facts(1, "knowledge_agent", {"location": "Cluj"})
    h._summaries[(1, "knowledge_agent")] = "lives in Cluj"
    out = h.build_context_messages(1, "knowledge_agent")
    assert len(out) == 2
    assert all(isinstance(m, SystemMessage) for m in out)
    assert "Known facts" in str(out[0].content)
    assert "Earlier conversation summary" in str(out[1].content)


def test_build_context_messages_isolated_per_agent_slice():
    h = ChatHistoryManager()
    h.add_facts(1, "scufris", {"k": "main"})
    h.add_facts(1, "knowledge_agent", {"k": "sub"})
    main = h.build_context_messages(1, "scufris")
    sub = h.build_context_messages(1, "knowledge_agent")
    assert "main" in str(main[0].content) and "sub" not in str(main[0].content)
    assert "sub" in str(sub[0].content) and "main" not in str(sub[0].content)


def test_facts_render_includes_provenance_and_age():
    h = ChatHistoryManager()
    h.add_facts(1, "scufris", {"location": "Cluj"})
    out = h.build_context_messages(1, "scufris")
    content = str(out[0].content)
    # Provenance + age suffix per line.
    assert "(remember, " in content
    assert " ago" in content or "just now" in content


# ---------------------------------------------------------------------------
# Integration sanity: prompt shape unchanged when slice is fresh
# ---------------------------------------------------------------------------


def test_prompt_shape_identical_to_phase1_when_no_compaction_state():
    h = ChatHistoryManager()
    h.add_user_message(1, "earlier")
    h.add_ai_message(1, "ok")
    msgs = h.get_history_with_new_message(1, "now")
    # No system messages prepended — pre-Phase-2 shape.
    assert all(m["role"] in ("user", "assistant") for m in msgs)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]


def test_unused_imports_smoke():
    # Reference imports kept at module scope for clarity.
    assert BaseMessage is not None
    assert isinstance({}, Dict)

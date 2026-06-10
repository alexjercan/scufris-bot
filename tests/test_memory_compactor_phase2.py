"""Tests for Phase 2 of the history-compaction rollout
(`tasks/20260510-183123`).

Covers:

- :class:`LLMCompactor` happy path + every degradation mode
  (transport raises, malformed JSON, non-object root, non-string
  fields).
- :func:`create_compactor` factory honouring the
  ``SCUFRIS_COMPACTOR=noop`` opt-out.
- ``ChatHistoryManager.get_history_with_new_message`` prepending
  facts + summary system messages in the documented order.
- ``ChatHistoryManager.build_context_messages`` for the sub-agent
  injection path.
- Per-fact provenance: ``FactEntry`` populated with source +
  timestamp; compactor-sourced facts marked ``"compactor"``;
  ``add_facts`` defaults to ``"remember"``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from utils.history import ChatHistoryManager
from utils.memory_compactor import (
    CompactionResult,
    FactEntry,
    LLMCompactor,
    NoopCompactor,
    create_compactor,
    format_age,
)
from utils.messages import HistoryMessage, user_message

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Minimal chat transport stub: configurable return value, records prompts.

    Mirrors the :class:`OllamaChatTransport` surface
    (``.chat(messages) -> str``) without any HTTP traffic.
    """

    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: List[List[Dict[str, str]]] = []

    def chat(self, messages: List[Dict[str, str]]) -> Any:
        self.calls.append(list(messages))
        return self._response

    @property
    def prompts(self) -> List[str]:
        """Helper for legacy assertion shape: extract user content."""
        return [
            m["content"] for call in self.calls for m in call if m["role"] == "user"
        ]


class _RaisingTransport:
    def chat(self, messages: List[Dict[str, str]]) -> Any:
        raise RuntimeError("transport down")


# ---------------------------------------------------------------------------
# LLMCompactor
# ---------------------------------------------------------------------------


def test_llm_compactor_parses_valid_json_response():
    payload = {"summary": "user lives in Cluj", "facts": {"location": "Cluj"}}
    transport = _FakeTransport(json.dumps(payload))
    c = LLMCompactor(transport)
    result = c.compact([user_message("I live in Cluj")], "", {})
    assert result == {"summary": "user lives in Cluj", "facts": {"location": "Cluj"}}
    assert "I live in Cluj" in transport.prompts[0]


def test_llm_compactor_accepts_plain_string_response():
    payload = '{"summary": "s", "facts": {"k": "v"}}'
    c = LLMCompactor(_FakeTransport(payload))
    assert c.compact([user_message("x")], "", {}) == {
        "summary": "s",
        "facts": {"k": "v"},
    }


def test_llm_compactor_strips_markdown_fences():
    fenced = "```json\n" + json.dumps({"summary": "s", "facts": {}}) + "\n```"
    c = LLMCompactor(_FakeTransport(fenced))
    assert c.compact([user_message("x")], "prev", {})["summary"] == "s"


def test_llm_compactor_malformed_json_preserves_existing_summary():
    c = LLMCompactor(_FakeTransport("not json at all {"))
    out = c.compact([user_message("x")], "kept", {})
    assert out == {"summary": "kept", "facts": {}}


def test_llm_compactor_non_object_root_preserves_existing_summary():
    c = LLMCompactor(_FakeTransport('["just", "a", "list"]'))
    assert c.compact([user_message("x")], "kept", {}) == {
        "summary": "kept",
        "facts": {},
    }


def test_llm_compactor_transport_raises_returns_noop_result():
    c = LLMCompactor(_RaisingTransport())
    assert c.compact([user_message("x")], "kept", {"k": "v"}) == {
        "summary": "kept",
        "facts": {},
    }


def test_llm_compactor_empty_evicted_short_circuits_without_calling_transport():
    transport = _FakeTransport("should not be used")
    c = LLMCompactor(transport)
    out = c.compact([], "kept", {})
    assert out == {"summary": "kept", "facts": {}}
    assert transport.calls == []


def test_llm_compactor_coerces_non_string_fact_values_and_drops_garbage():
    payload = {
        "summary": "ok",
        "facts": {"age": 30, "active": True, "ratio": 0.5, "bad": [1, 2]},
    }
    c = LLMCompactor(_FakeTransport(json.dumps(payload)))
    out = c.compact([user_message("x")], "", {})
    # Scalars coerced to str; list value dropped.
    assert out["facts"] == {"age": "30", "active": "True", "ratio": "0.5"}


def test_llm_compactor_non_string_summary_field_falls_back_to_existing():
    payload = {"summary": 123, "facts": {"k": "v"}}
    c = LLMCompactor(_FakeTransport(json.dumps(payload)))
    out = c.compact([user_message("x")], "prev", {})
    assert out["summary"] == "prev"
    assert out["facts"] == {"k": "v"}


# ---------------------------------------------------------------------------
# create_compactor factory
# ---------------------------------------------------------------------------


def test_create_compactor_noop_env_returns_noop(monkeypatch):
    monkeypatch.setenv("SCUFRIS_COMPACTOR", "noop")
    assert isinstance(create_compactor(), NoopCompactor)


def test_create_compactor_with_explicit_transport_skips_ollama_construction(
    monkeypatch,
):
    monkeypatch.delenv("SCUFRIS_COMPACTOR", raising=False)
    fake = _FakeTransport('{"summary":"","facts":{}}')
    c = create_compactor(transport=fake)
    assert isinstance(c, LLMCompactor)
    # Sanity: it's actually wired to our fake.
    c.compact([user_message("x")], "", {})
    assert len(fake.calls) == 1


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
    assert all(isinstance(m, HistoryMessage) and m.role == "system" for m in out)
    assert "Known facts" in out[0].content
    assert "Earlier conversation summary" in out[1].content


def test_build_context_messages_isolated_per_agent_slice():
    h = ChatHistoryManager()
    h.add_facts(1, "scufris", {"k": "main"})
    h.add_facts(1, "knowledge_agent", {"k": "sub"})
    main = h.build_context_messages(1, "scufris")
    sub = h.build_context_messages(1, "knowledge_agent")
    assert "main" in main[0].content and "sub" not in main[0].content
    assert "sub" in sub[0].content and "main" not in sub[0].content


def test_facts_render_includes_provenance_and_age():
    h = ChatHistoryManager()
    h.add_facts(1, "scufris", {"location": "Cluj"})
    out = h.build_context_messages(1, "scufris")
    content = out[0].content
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

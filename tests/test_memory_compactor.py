"""Unit tests for utils.memory_compactor + ChatHistoryManager compaction wiring.

Covers Phase 1 of the history-compaction rollout
(`tasks/20260510-183121`):

- The :class:`Compactor` Protocol contract via :class:`NoopCompactor`.
- New ``_summaries`` and ``_facts`` storage on
  :class:`ChatHistoryManager` and its accessors.
- The eviction → compactor wiring in ``_trim_history`` and
  ``_trim_by_tokens``.
- Summary/facts caps + last-write-wins semantics.
- Behaviour preservation: with the default ``NoopCompactor``, the
  observable history behaviour is identical to pre-compaction.
"""

from typing import Dict, List

from utils.history import (
    _FACTS_ENTRY_CAP,
    _SUMMARY_CHAR_CAP,
    ChatHistoryManager,
)
from utils.memory_compactor import (
    CompactionResult,
    NoopCompactor,
)
from utils.messages import HistoryMessage, user_message

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class RecordingCompactor:
    """Records every compact() call; returns a configurable result."""

    def __init__(self, result: CompactionResult | None = None) -> None:
        self.calls: list[tuple[list[HistoryMessage], str, Dict[str, str]]] = []
        self._result: CompactionResult = result or {
            "summary": "",
            "facts": {},
        }

    def compact(
        self,
        evicted: List[HistoryMessage],
        existing_summary: str,
        existing_facts: Dict[str, str],
    ) -> CompactionResult:
        self.calls.append((list(evicted), existing_summary, dict(existing_facts)))
        return self._result


class RaisingCompactor:
    """Always raises; used to verify error handling."""

    def compact(self, *_a, **_kw) -> CompactionResult:
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# NoopCompactor contract
# ---------------------------------------------------------------------------


def test_noop_preserves_existing_summary_and_returns_empty_facts():
    c = NoopCompactor()
    result = c.compact([user_message("x")], "prior summary", {"k": "v"})
    assert result == {"summary": "prior summary", "facts": {}}


def test_noop_handles_empty_inputs():
    c = NoopCompactor()
    assert c.compact([], "", {}) == {"summary": "", "facts": {}}


# ---------------------------------------------------------------------------
# ChatHistoryManager: compactor injection
# ---------------------------------------------------------------------------


def test_default_compactor_is_noop():
    h = ChatHistoryManager()
    assert isinstance(h._compactor, NoopCompactor)


def test_custom_compactor_is_used():
    rec = RecordingCompactor()
    h = ChatHistoryManager(compactor=rec)
    assert h._compactor is rec


# ---------------------------------------------------------------------------
# Accessors: summary + facts
# ---------------------------------------------------------------------------


def test_get_summary_returns_empty_for_unknown_slice():
    h = ChatHistoryManager()
    assert h.get_summary(1, "knowledge_agent") == ""


def test_get_facts_returns_empty_dict_copy():
    h = ChatHistoryManager()
    facts = h.get_facts(1, "knowledge_agent")
    facts["mutation"] = "should not leak"
    assert h.get_facts(1, "knowledge_agent") == {}


def test_add_facts_last_write_wins_on_collision():
    h = ChatHistoryManager()
    h.add_facts(1, "knowledge_agent", {"location": "Bucharest"})
    h.add_facts(1, "knowledge_agent", {"location": "Cluj"})
    assert h.get_facts(1, "knowledge_agent") == {"location": "Cluj"}


def test_add_facts_merges_distinct_keys():
    h = ChatHistoryManager()
    h.add_facts(1, "knowledge_agent", {"location": "Bucharest"})
    h.add_facts(1, "knowledge_agent", {"diet": "vegetarian"})
    assert h.get_facts(1, "knowledge_agent") == {
        "location": "Bucharest",
        "diet": "vegetarian",
    }


def test_add_facts_ignores_empty_dict():
    h = ChatHistoryManager()
    h.add_facts(1, "knowledge_agent", {"k": "v"})
    h.add_facts(1, "knowledge_agent", {})
    assert h.get_facts(1, "knowledge_agent") == {"k": "v"}


def test_add_facts_caps_at_entry_limit_dropping_oldest():
    h = ChatHistoryManager()
    for i in range(_FACTS_ENTRY_CAP + 5):
        h.add_facts(1, "knowledge_agent", {f"k{i}": f"v{i}"})
    facts = h.get_facts(1, "knowledge_agent")
    assert len(facts) == _FACTS_ENTRY_CAP
    # Oldest 5 dropped: k0..k4 gone, k5..k(N+4) kept.
    assert "k0" not in facts and "k4" not in facts
    assert "k5" in facts


def test_remove_fact_returns_true_when_present():
    h = ChatHistoryManager()
    h.add_facts(1, "knowledge_agent", {"k": "v"})
    assert h.remove_fact(1, "knowledge_agent", "k") is True
    assert h.get_facts(1, "knowledge_agent") == {}


def test_remove_fact_returns_false_when_absent():
    h = ChatHistoryManager()
    assert h.remove_fact(1, "knowledge_agent", "ghost") is False


# ---------------------------------------------------------------------------
# Eviction → compactor wiring (_trim_history, message-count cap)
# ---------------------------------------------------------------------------


def test_trim_history_calls_compactor_with_evicted_batch():
    rec = RecordingCompactor()
    h = ChatHistoryManager(max_history_per_user=3, compactor=rec)
    for i in range(5):
        h.add_user_message(1, f"msg{i}")
    # Window is 3; 2 messages should be evicted in two add_* calls
    # that bumped the cap. Compactor should have been called for
    # each eviction round.
    assert len(rec.calls) == 2
    # Each call evicts exactly 1 message (FIFO).
    assert [len(call[0]) for call in rec.calls] == [1, 1]
    assert rec.calls[0][0][0].content == "msg0"
    assert rec.calls[1][0][0].content == "msg1"


def test_trim_history_below_cap_does_not_call_compactor():
    rec = RecordingCompactor()
    h = ChatHistoryManager(max_history_per_user=10, compactor=rec)
    h.add_user_message(1, "alone")
    assert rec.calls == []


# ---------------------------------------------------------------------------
# Eviction → compactor wiring (_trim_by_tokens, sub-agent path)
# ---------------------------------------------------------------------------


def test_trim_by_tokens_calls_compactor_with_full_batch():
    rec = RecordingCompactor()
    h = ChatHistoryManager(compactor=rec)
    # Each message is ~40 chars → ~10 tokens (char/4 ratio).
    msgs = [user_message("x" * 40) for _ in range(5)]
    # Budget = 15 tokens (~60 chars). Should evict 4 of 5
    # (always keep 1 minimum).
    h.add_messages(1, "knowledge_agent", msgs, token_budget=15)
    assert len(rec.calls) == 1
    evicted, _, _ = rec.calls[0]
    assert len(evicted) == 4
    assert h.get_message_count(1, "knowledge_agent") == 1


def test_trim_by_tokens_no_eviction_skips_compactor():
    rec = RecordingCompactor()
    h = ChatHistoryManager(compactor=rec)
    h.add_messages(1, "knowledge_agent", [user_message("x")], token_budget=1000)
    assert rec.calls == []


# ---------------------------------------------------------------------------
# Compactor result merging into _summaries / _facts
# ---------------------------------------------------------------------------


def test_compactor_result_summary_replaces_existing():
    rec = RecordingCompactor(result={"summary": "user lives in Cluj", "facts": {}})
    h = ChatHistoryManager(max_history_per_user=1, compactor=rec)
    h.add_user_message(1, "first")
    h.add_user_message(1, "second")  # triggers eviction
    assert h.get_summary(1) == "user lives in Cluj"


def test_compactor_result_facts_merge_into_slice():
    rec = RecordingCompactor(result={"summary": "", "facts": {"location": "Bucharest"}})
    h = ChatHistoryManager(max_history_per_user=1, compactor=rec)
    h.add_user_message(1, "first")
    h.add_user_message(1, "second")
    assert h.get_facts(1) == {"location": "Bucharest"}


def test_summary_cap_clips_with_ellipsis_on_overflow():
    long_summary = "x" * (_SUMMARY_CHAR_CAP + 200)
    rec = RecordingCompactor(result={"summary": long_summary, "facts": {}})
    h = ChatHistoryManager(max_history_per_user=1, compactor=rec)
    h.add_user_message(1, "a")
    h.add_user_message(1, "b")
    summary = h.get_summary(1)
    assert len(summary) == _SUMMARY_CHAR_CAP
    assert summary.endswith("…")


def test_compactor_exception_is_swallowed_and_eviction_proceeds():
    h = ChatHistoryManager(max_history_per_user=2, compactor=RaisingCompactor())
    h.add_user_message(1, "a")
    h.add_user_message(1, "b")
    h.add_user_message(1, "c")  # would evict "a"; compactor raises
    # Window still trimmed to 2 — eviction must succeed even if
    # compaction fails.
    assert h.get_message_count(1) == 2
    # Summary stays empty because the salvage attempt failed.
    assert h.get_summary(1) == ""


def test_noop_compactor_leaves_summary_and_facts_empty():
    h = ChatHistoryManager(max_history_per_user=1, compactor=NoopCompactor())
    h.add_user_message(1, "first")
    h.add_user_message(1, "second")  # triggers eviction
    assert h.get_summary(1) == ""
    assert h.get_facts(1) == {}


# ---------------------------------------------------------------------------
# clear_user wipes the new layers
# ---------------------------------------------------------------------------


def test_clear_user_wipes_summaries():
    h = ChatHistoryManager()
    h._summaries[(1, "knowledge_agent")] = "stale"
    h.clear_user(1)
    assert h.get_summary(1, "knowledge_agent") == ""


def test_clear_user_wipes_facts():
    h = ChatHistoryManager()
    h.add_facts(1, "knowledge_agent", {"k": "v"})
    h.clear_user(1)
    assert h.get_facts(1, "knowledge_agent") == {}


def test_clear_user_does_not_affect_other_users():
    h = ChatHistoryManager()
    h.add_facts(1, "knowledge_agent", {"k": "v1"})
    h.add_facts(2, "knowledge_agent", {"k": "v2"})
    h.clear_user(1)
    assert h.get_facts(2, "knowledge_agent") == {"k": "v2"}


# ---------------------------------------------------------------------------
# Behaviour preservation: NoopCompactor + history acts like before
# ---------------------------------------------------------------------------


def test_message_count_after_eviction_with_noop_matches_legacy_behaviour():
    h = ChatHistoryManager(max_history_per_user=3, compactor=NoopCompactor())
    for i in range(10):
        h.add_user_message(1, f"msg{i}")
    assert h.get_message_count(1) == 3
    # Most recent 3 retained, oldest 7 dropped.
    assert [m.content for m in h.get_history(1)] == ["msg7", "msg8", "msg9"]

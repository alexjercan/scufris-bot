"""Unit tests for utils.history.ChatHistoryManager.

Covers Phase 3.1 (per-agent slicing), Phase 3.3 (token-budget trim),
the defaultdict phantom-entry regression, and stats aggregation.
"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from utils.history import SCUFRIS_AGENT, ChatHistoryManager, create_history_manager

# ---------------------------------------------------------------------------
# Backward compatibility: agent defaults to "scufris"
# ---------------------------------------------------------------------------


def test_get_history_default_agent_matches_scufris_slot():
    h = ChatHistoryManager()
    h.add_user_message(1, "hello")
    assert h.get_history(1) == h.get_history(1, SCUFRIS_AGENT)


def test_add_user_and_ai_messages_round_trip_in_order():
    h = ChatHistoryManager()
    h.add_user_message(1, "hi")
    h.add_ai_message(1, "hello")
    msgs = h.get_history(1)
    assert [type(m) for m in msgs] == [HumanMessage, AIMessage]
    assert [m.content for m in msgs] == ["hi", "hello"]


def test_get_history_with_new_message_appends_user_turn_dict_format():
    h = ChatHistoryManager()
    h.add_user_message(1, "hi")
    h.add_ai_message(1, "hello")
    out = h.get_history_with_new_message(1, "again")
    assert out == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "again"},
    ]


def test_message_count_default_agent_only():
    h = ChatHistoryManager()
    h.add_user_message(1, "hi")
    h.add_messages(1, "knowledge_agent", [HumanMessage(content="x")], token_budget=100)
    assert h.get_message_count(1) == 1
    assert h.get_message_count(1, "knowledge_agent") == 1


# ---------------------------------------------------------------------------
# Per-agent isolation
# ---------------------------------------------------------------------------


def test_slices_are_isolated_across_agents_for_same_user():
    h = ChatHistoryManager()
    h.add_user_message(1, "main")
    h.add_messages(1, "knowledge_agent", [HumanMessage(content="k")], token_budget=100)
    assert [m.content for m in h.get_history(1)] == ["main"]
    assert [m.content for m in h.get_history(1, "knowledge_agent")] == ["k"]


def test_slices_are_isolated_across_users_for_same_agent():
    h = ChatHistoryManager()
    h.add_user_message(1, "u1")
    h.add_user_message(2, "u2")
    assert [m.content for m in h.get_history(1)] == ["u1"]
    assert [m.content for m in h.get_history(2)] == ["u2"]


def test_clear_user_wipes_all_per_agent_slices_for_that_user():
    h = ChatHistoryManager()
    h.add_user_message(1, "main")
    h.add_messages(1, "knowledge_agent", [HumanMessage(content="k")], token_budget=100)
    h.add_user_message(2, "other")
    removed = h.clear_user(1)
    assert removed == 2
    assert h.get_history(1) == []
    assert h.get_history(1, "knowledge_agent") == []
    assert [m.content for m in h.get_history(2)] == ["other"]


def test_clear_history_is_alias_for_clear_user():
    h = ChatHistoryManager()
    h.add_user_message(1, "x")
    assert h.clear_history(1) == 1


# ---------------------------------------------------------------------------
# Phantom-entry regression: get_history must not create empty slices
# ---------------------------------------------------------------------------


def test_get_history_does_not_create_phantom_slice():
    h = ChatHistoryManager()
    _ = h.get_history(1, "knowledge_agent")
    _ = h.get_message_count(1)
    _ = h.get_token_estimate(1, "knowledge_agent")
    assert h.get_user_count() == 0
    assert h.get_user_breakdown(1) == {}


def test_get_user_breakdown_omits_empty_slices():
    h = ChatHistoryManager()
    h.add_user_message(1, "hi")
    h.add_messages(1, "knowledge_agent", [], token_budget=100)
    bd = h.get_user_breakdown(1)
    assert bd == {SCUFRIS_AGENT: 1}


# ---------------------------------------------------------------------------
# Main-flow message-count cap
# ---------------------------------------------------------------------------


def test_main_flow_trims_to_max_history_per_user():
    h = ChatHistoryManager(max_history_per_user=3)
    for i in range(5):
        h.add_user_message(1, f"m{i}")
    msgs = h.get_history(1)
    assert len(msgs) == 3
    assert [m.content for m in msgs] == ["m2", "m3", "m4"]


# ---------------------------------------------------------------------------
# Token-budget trim (sub-agent slices)
# ---------------------------------------------------------------------------


def test_add_messages_no_op_for_empty_list():
    h = ChatHistoryManager()
    h.add_messages(1, "knowledge_agent", [], token_budget=100)
    assert h.get_history(1, "knowledge_agent") == []


def test_token_trim_evicts_oldest_first_fifo():
    h = ChatHistoryManager()
    msgs = [HumanMessage(content="a" * 40), HumanMessage(content="b" * 40)]
    # budget = 15 tokens => 60 chars. Both 40-char msgs would be 80 chars.
    h.add_messages(1, "ka", msgs, token_budget=15)
    kept = h.get_history(1, "ka")
    assert [m.content for m in kept] == ["b" * 40]


def test_token_trim_preserves_message_boundaries():
    h = ChatHistoryManager()
    long = HumanMessage(content="x" * 1000)
    h.add_messages(1, "ka", [long], token_budget=10)
    kept = h.get_history(1, "ka")
    # Never-empty invariant: even when single msg exceeds budget,
    # it stays whole rather than being split.
    assert len(kept) == 1
    assert kept[0].content == "x" * 1000


def test_token_trim_never_empties_slice():
    h = ChatHistoryManager()
    h.add_messages(
        1,
        "ka",
        [HumanMessage(content="a" * 100), HumanMessage(content="b" * 100)],
        token_budget=0,
    )
    assert len(h.get_history(1, "ka")) == 1


def test_token_trim_preserves_basemessage_subtypes():
    h = ChatHistoryManager()
    h.add_messages(
        1,
        "ka",
        [
            SystemMessage(content="sys"),
            HumanMessage(content="u"),
            AIMessage(content="a"),
        ],
        token_budget=1000,
    )
    kept = h.get_history(1, "ka")
    assert [type(m) for m in kept] == [SystemMessage, HumanMessage, AIMessage]


# ---------------------------------------------------------------------------
# Telemetry + stats
# ---------------------------------------------------------------------------


def test_record_invocation_increments_and_timestamps():
    h = ChatHistoryManager()
    h.record_invocation(1, "knowledge_agent")
    h.record_invocation(1, "knowledge_agent")
    tel = h.get_user_telemetry(1)
    assert tel["knowledge_agent"]["invocations"] == 2
    assert tel["knowledge_agent"]["last_activity"] is not None


def test_register_agent_surfaces_in_telemetry_with_zero_traffic():
    h = ChatHistoryManager()
    h.register_agent("knowledge_agent", token_budget=4000, model="qwen3")
    tel = h.get_user_telemetry(1)
    assert tel["knowledge_agent"]["budget"] == 4000
    assert tel["knowledge_agent"]["model"] == "qwen3"
    assert tel["knowledge_agent"]["invocations"] == 0
    assert tel["knowledge_agent"]["messages"] == 0


def test_get_stats_aggregates_across_users_and_agents():
    h = ChatHistoryManager(max_history_per_user=20)
    h.add_user_message(1, "u1-main")
    h.add_user_message(2, "u2-main")
    h.add_messages(1, "knowledge_agent", [HumanMessage(content="k")], token_budget=100)
    h.record_invocation(1, "knowledge_agent")
    stats = h.get_stats()
    assert stats["total_users"] == 2
    assert stats["total_messages"] == 3
    assert stats["max_history_per_user"] == 20
    assert stats["messages_per_agent"] == {SCUFRIS_AGENT: 2, "knowledge_agent": 1}
    assert stats["total_invocations"] == 1


def test_get_token_estimate_uses_chars_per_token_proxy():
    h = ChatHistoryManager()
    # 80 chars / 4 chars-per-token = 20
    h.add_user_message(1, "a" * 80)
    assert h.get_token_estimate(1) == 20


def test_create_history_manager_returns_configured_instance():
    h = create_history_manager(max_history_per_user=7)
    assert isinstance(h, ChatHistoryManager)
    assert h.max_history_per_user == 7


# ---------------------------------------------------------------------------
# Per-tool invocation counter (for /stats histogram)
# ---------------------------------------------------------------------------


def test_record_tool_invocation_aggregates_per_user():
    h = ChatHistoryManager()
    h.record_tool_invocation(1, "web_search")
    h.record_tool_invocation(1, "web_search")
    h.record_tool_invocation(1, "weather")
    h.record_tool_invocation(2, "web_search")
    assert h.get_tool_invocations(1) == {"web_search": 2, "weather": 1}
    assert h.get_tool_invocations(2) == {"web_search": 1}


def test_get_tool_invocations_empty_for_unseen_user():
    h = ChatHistoryManager()
    assert h.get_tool_invocations(99) == {}


def test_clear_user_preserves_tool_invocations():
    """Tool counters mirror ``_invocations`` semantics: traffic, not memory."""
    h = ChatHistoryManager()
    h.add_user_message(1, "hi")
    h.record_tool_invocation(1, "web_search")
    h.record_tool_invocation(1, "web_search")
    h.clear_user(1)
    assert h.get_tool_invocations(1) == {"web_search": 2}

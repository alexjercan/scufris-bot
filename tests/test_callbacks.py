"""Unit tests for utils.callbacks parsers + ThinkingEvent dataclass.

The depth-aware ``ToolCallbackHandler`` (and its lifecycle / telemetry
hand-off tests) lived here pre-OpenCode. After
``tasks/20260610-101413`` the handler is gone — the runtime emits
``ThinkingEvent`` instances directly while consuming OpenCode's SSE
event stream — so the lifecycle suite was deleted along with the
class. What stays is exercise of the small set of pure helpers that
the OpenCode listener still imports.
"""

import pytest

from utils.callbacks import (
    DISPLAY_NAMES,
    SUB_AGENT_NAMES,
    ThinkingEvent,
    _parse_tool_arg,
    _parse_tool_context,
    display_name,
    is_sub_agent,
)

# ---------------------------------------------------------------------------
# display_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("technical,expected", list(DISPLAY_NAMES.items()))
def test_display_name_known_via_table(technical, expected):
    assert display_name(technical) == expected


def test_display_name_unknown_falls_back_to_title_case():
    assert display_name("foo_bar_baz") == "Foo Bar Baz"


# ---------------------------------------------------------------------------
# is_sub_agent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SUB_AGENT_NAMES))
def test_is_sub_agent_true_for_known(name):
    assert is_sub_agent(name) is True


def test_is_sub_agent_true_for_anything_ending_in_agent():
    assert is_sub_agent("foo_agent") is True


@pytest.mark.parametrize("name", ["weather", "calculator_tool", "opencode", ""])
def test_is_sub_agent_false_for_leaf_tools(name):
    assert is_sub_agent(name) is False


# ---------------------------------------------------------------------------
# _parse_tool_arg
# ---------------------------------------------------------------------------


def test_parse_arg_json_with_query_returns_query():
    assert (
        _parse_tool_arg('{"query": "weather in Bucharest"}') == "weather in Bucharest"
    )


def test_parse_arg_json_with_double_underscore_arg1():
    assert _parse_tool_arg('{"__arg1": "Ploiesti"}') == "Ploiesti"


def test_parse_arg_json_first_scalar_when_no_preferred_key():
    assert _parse_tool_arg('{"random": "val", "other": "ignored"}') == "val"


def test_parse_arg_python_repr_via_literal_eval():
    assert _parse_tool_arg("{'__arg1': 'Ploiesti'}") == "Ploiesti"


def test_parse_arg_bare_string_returns_as_is():
    assert _parse_tool_arg("just a string") == "just a string"


def test_parse_arg_empty_returns_none():
    assert _parse_tool_arg("") is None
    assert _parse_tool_arg("   ") is None


def test_parse_arg_prefers_query_over_other_keys():
    assert _parse_tool_arg('{"context": "ctx", "query": "real"}') == "real"


# ---------------------------------------------------------------------------
# _parse_tool_context
# ---------------------------------------------------------------------------


def test_parse_context_returns_non_empty_value():
    assert _parse_tool_context('{"query": "q", "context": "RO"}') == "RO"


def test_parse_context_empty_string_returns_none():
    assert _parse_tool_context('{"query": "q", "context": ""}') is None
    assert _parse_tool_context('{"query": "q", "context": "  "}') is None


def test_parse_context_missing_key_returns_none():
    assert _parse_tool_context('{"query": "q"}') is None


def test_parse_context_non_dict_returns_none():
    assert _parse_tool_context('"just a string"') is None
    assert _parse_tool_context("[1,2,3]") is None


def test_parse_context_unparseable_returns_none():
    assert _parse_tool_context("not json or python") is None


def test_parse_context_empty_input_returns_none():
    assert _parse_tool_context("") is None


# ---------------------------------------------------------------------------
# ThinkingEvent dataclass
# ---------------------------------------------------------------------------


def test_thinking_event_dataclass_defaults():
    ev = ThinkingEvent(kind="text", source="main", text="hi", depth=0)
    assert ev.arg is None
    assert ev.context is None
    assert ev.prior_turns is None
    assert ev.evicted is None
    assert ev.new_facts is None


def test_thinking_event_carries_compaction_fields():
    ev = ThinkingEvent(
        kind="compaction",
        source="knowledge_agent",
        text="knowledge_agent",
        depth=0,
        evicted=3,
        new_facts=2,
    )
    assert ev.evicted == 3
    assert ev.new_facts == 2

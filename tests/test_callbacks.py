"""Unit tests for utils.callbacks parsers + ToolCallbackHandler lifecycle."""

import json
from uuid import uuid4

import pytest

from utils import telemetry
from utils.callbacks import (
    DISPLAY_NAMES,
    SUB_AGENT_NAMES,
    ThinkingEvent,
    ToolCallbackHandler,
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
# ToolCallbackHandler lifecycle + telemetry handoff
# ---------------------------------------------------------------------------


class _FakeOutput:
    def __init__(self, content, status="ok"):
        self.content = content
        self.status = status


@pytest.fixture
def tmp_telemetry(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_path = log_dir / "sub_agent_telemetry.jsonl"
    monkeypatch.setattr(telemetry, "_LOG_DIR", log_dir)
    monkeypatch.setattr(telemetry, "_LOG_PATH", log_path)
    monkeypatch.setenv("SCUFRIS_TELEMETRY", "1")
    return log_path


def _start_sub_agent_run(handler, name="knowledge_agent", input_dict=None, run_id=None):
    run_id = run_id or uuid4()
    payload = json.dumps(input_dict or {"query": "weather", "context": "RO"})
    handler.on_tool_start({"name": name}, payload, run_id=run_id)
    return run_id


def test_on_tool_start_emits_tool_call_with_arg_and_context():
    events = []
    h = ToolCallbackHandler(on_thinking=events.append)
    _start_sub_agent_run(h)
    [ev] = [e for e in events if e.kind == "tool_call"]
    assert ev.text == "knowledge_agent"
    assert ev.arg == "weather"
    assert ev.context == "RO"


def test_on_tool_end_emits_telemetry_with_outcome_ok(tmp_telemetry):
    h = ToolCallbackHandler()
    rid = _start_sub_agent_run(h)
    h.on_tool_end(_FakeOutput("everything fine"), run_id=rid)
    rec = json.loads(tmp_telemetry.read_text().splitlines()[0])
    assert rec["outcome"] == "ok"
    assert rec["child_agent"] == "knowledge_agent"
    assert rec["query_chars"] == len("weather")
    assert rec["context_chars"] == len("RO")


def test_on_tool_end_emits_telemetry_with_outcome_refused(tmp_telemetry):
    h = ToolCallbackHandler()
    rid = _start_sub_agent_run(h)
    h.on_tool_end(_FakeOutput("cannot_handle: nope"), run_id=rid)
    rec = json.loads(tmp_telemetry.read_text().splitlines()[0])
    assert rec["outcome"] == "refused"


def test_on_tool_error_emits_telemetry_with_outcome_error(tmp_telemetry):
    h = ToolCallbackHandler()
    rid = _start_sub_agent_run(h)
    h.on_tool_error(RuntimeError("boom"), run_id=rid)
    rec = json.loads(tmp_telemetry.read_text().splitlines()[0])
    assert rec["outcome"] == "error"


def test_leaf_tool_does_not_emit_telemetry(tmp_telemetry):
    h = ToolCallbackHandler()
    rid = uuid4()
    h.on_tool_start({"name": "weather"}, '{"location": "Bucharest"}', run_id=rid)
    h.on_tool_end(_FakeOutput("sunny"), run_id=rid)
    assert not tmp_telemetry.exists()


def test_on_tool_end_unknown_run_id_is_a_noop():
    h = ToolCallbackHandler()
    # Should not raise.
    h.on_tool_end(_FakeOutput("x"), run_id=uuid4())


def test_emit_prior_turns_emits_tool_meta_event():
    events = []
    h = ToolCallbackHandler(on_thinking=events.append)
    _start_sub_agent_run(h)  # registers a run; rid not needed here
    events.clear()
    h.emit_prior_turns("knowledge_agent", count=3)
    [ev] = events
    assert ev.kind == "tool_meta"
    assert ev.text == "knowledge_agent"
    assert ev.prior_turns == 3


def test_emit_prior_turns_no_op_for_zero_count():
    events = []
    h = ToolCallbackHandler(on_thinking=events.append)
    _start_sub_agent_run(h)
    events.clear()
    h.emit_prior_turns("knowledge_agent", count=0)
    assert events == []


def test_thinking_event_dataclass_defaults():
    ev = ThinkingEvent(kind="text", source="main", text="hi", depth=0)
    assert ev.arg is None
    assert ev.context is None
    assert ev.prior_turns is None

"""Unit tests for utils.telemetry."""

import json

import pytest

from utils import telemetry

# ---------------------------------------------------------------------------
# is_enabled()
# ---------------------------------------------------------------------------


def test_is_enabled_false_when_unset(monkeypatch):
    monkeypatch.delenv("SCUFRIS_TELEMETRY", raising=False)
    assert telemetry.is_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", "ON"])
def test_is_enabled_true_for_truthy_values(monkeypatch, value):
    monkeypatch.setenv("SCUFRIS_TELEMETRY", value)
    assert telemetry.is_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "nope", ""])
def test_is_enabled_false_for_falsy_values(monkeypatch, value):
    monkeypatch.setenv("SCUFRIS_TELEMETRY", value)
    assert telemetry.is_enabled() is False


# ---------------------------------------------------------------------------
# is_refusal()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output",
    [
        "cannot_handle: foo",
        "  CANNOT_HANDLE: bar",
        "\n\ncannot_handle: leading newlines",
        "Cannot_Handle: mixed case",
    ],
)
def test_is_refusal_true_for_documented_prefix(output):
    assert telemetry.is_refusal(output) is True


@pytest.mark.parametrize("output", ["", "ok", "all good", "no refusal here"])
def test_is_refusal_false_for_normal_output(output):
    assert telemetry.is_refusal(output) is False


@pytest.mark.parametrize("output", [None, 42, [], {"x": 1}])
def test_is_refusal_false_for_non_string(output):
    assert telemetry.is_refusal(output) is False


# ---------------------------------------------------------------------------
# begin_turn() contextvars
# ---------------------------------------------------------------------------


def test_begin_turn_binds_user_and_turn_id():
    with telemetry.begin_turn("telegram:42") as tid:
        assert telemetry.current_user_id() == "telegram:42"
        assert telemetry.current_turn_id() == tid
        assert isinstance(tid, str) and len(tid) > 0


def test_begin_turn_resets_on_exit():
    with telemetry.begin_turn("u1"):
        pass
    assert telemetry.current_user_id() is None
    assert telemetry.current_turn_id() is None


def test_begin_turn_nested_restores_outer_on_inner_exit():
    with telemetry.begin_turn("outer") as outer_tid:
        with telemetry.begin_turn("inner") as inner_tid:
            assert telemetry.current_user_id() == "inner"
            assert telemetry.current_turn_id() == inner_tid
        assert telemetry.current_user_id() == "outer"
        assert telemetry.current_turn_id() == outer_tid


# ---------------------------------------------------------------------------
# log_sub_agent_event()
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_log(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_path = log_dir / "sub_agent_telemetry.jsonl"
    monkeypatch.setattr(telemetry, "_LOG_DIR", log_dir)
    monkeypatch.setattr(telemetry, "_LOG_PATH", log_path)
    return log_path


def test_log_event_no_op_when_disabled(monkeypatch, tmp_log):
    monkeypatch.delenv("SCUFRIS_TELEMETRY", raising=False)
    telemetry.log_sub_agent_event(
        child_agent="knowledge_agent",
        query_chars=10,
        context_chars=0,
        outcome="ok",
        duration_ms=5,
    )
    assert not tmp_log.exists()


def test_log_event_writes_one_valid_jsonl_record(monkeypatch, tmp_log):
    monkeypatch.setenv("SCUFRIS_TELEMETRY", "1")
    with telemetry.begin_turn("cli:local"):
        telemetry.log_sub_agent_event(
            child_agent="knowledge_agent",
            query_chars=42,
            context_chars=7,
            outcome="ok",
            duration_ms=123,
        )
    lines = tmp_log.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["child_agent"] == "knowledge_agent"
    assert rec["query_chars"] == 42
    assert rec["context_chars"] == 7
    assert rec["context_present"] is True
    assert rec["outcome"] == "ok"
    assert rec["duration_ms"] == 123
    assert rec["parent_agent"] == "scufris"
    assert rec["user_id"] == "cli:local"
    assert rec["turn_id"] is not None
    assert "ts" in rec


def test_log_event_context_present_false_when_zero_chars(monkeypatch, tmp_log):
    monkeypatch.setenv("SCUFRIS_TELEMETRY", "1")
    telemetry.log_sub_agent_event(
        child_agent="knowledge_agent",
        query_chars=10,
        context_chars=0,
        outcome="ok",
        duration_ms=1,
    )
    rec = json.loads(tmp_log.read_text().splitlines()[0])
    assert rec["context_present"] is False


def test_log_event_appends_across_calls(monkeypatch, tmp_log):
    monkeypatch.setenv("SCUFRIS_TELEMETRY", "1")
    for i in range(3):
        telemetry.log_sub_agent_event(
            child_agent="x",
            query_chars=i,
            context_chars=0,
            outcome="ok",
            duration_ms=1,
        )
    assert len(tmp_log.read_text().splitlines()) == 3


def test_log_event_swallows_write_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("SCUFRIS_TELEMETRY", "1")
    # Point at a path whose parent cannot be created (a regular file).
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(telemetry, "_LOG_DIR", blocker / "subdir")
    monkeypatch.setattr(telemetry, "_LOG_PATH", blocker / "subdir" / "x.jsonl")
    # Must not raise.
    telemetry.log_sub_agent_event(
        child_agent="x",
        query_chars=1,
        context_chars=0,
        outcome="ok",
        duration_ms=1,
    )


# ---------------------------------------------------------------------------
# _rotate_if_needed()
# ---------------------------------------------------------------------------


def test_rotate_no_op_when_below_threshold(monkeypatch, tmp_log):
    monkeypatch.setattr(telemetry, "_ROTATE_BYTES", 1024)
    tmp_log.parent.mkdir(parents=True, exist_ok=True)
    tmp_log.write_text("small\n")
    telemetry._rotate_if_needed()
    assert tmp_log.exists()
    assert not tmp_log.with_suffix(tmp_log.suffix + ".1").exists()


def test_rotate_renames_when_over_threshold(monkeypatch, tmp_log):
    monkeypatch.setattr(telemetry, "_ROTATE_BYTES", 10)
    tmp_log.parent.mkdir(parents=True, exist_ok=True)
    tmp_log.write_text("x" * 100)
    telemetry._rotate_if_needed()
    rotated = tmp_log.with_suffix(tmp_log.suffix + ".1")
    assert rotated.exists()
    assert not tmp_log.exists()
    assert rotated.read_text() == "x" * 100


def test_rotate_overwrites_existing_dot1(monkeypatch, tmp_log):
    monkeypatch.setattr(telemetry, "_ROTATE_BYTES", 10)
    tmp_log.parent.mkdir(parents=True, exist_ok=True)
    rotated = tmp_log.with_suffix(tmp_log.suffix + ".1")
    rotated.write_text("OLD")
    tmp_log.write_text("y" * 100)
    telemetry._rotate_if_needed()
    assert rotated.read_text() == "y" * 100


def test_rotate_no_op_when_no_log_file(tmp_log):
    # File doesn't exist; should not raise.
    telemetry._rotate_if_needed()
    assert not tmp_log.exists()

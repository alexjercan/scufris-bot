"""Tests for :mod:`scufris_server.event_mapping` (step 3 of #11).

Table-driven coverage of the opencode → :class:`ThinkingEvent`
mapping. The mapper is pure / stateless apart from per-part-id
dedup, so the tests construct event dicts in-line (no fixtures, no
opencode connection) and assert against the resulting events.

Each test names what it pins; reading the file top-to-bottom should
reproduce the mapping table from the module docstring.
"""

from __future__ import annotations

from typing import Any

from scufris_server.event_mapping import (
    EventMapperState,
    extract_text_delta,
    map_opencode_event,
)
from scufris_server.events import SCUFRIS_SOURCE, ThinkingEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _delta_event(delta: str, *, field_name: str = "text") -> dict[str, Any]:
    """Construct a ``message.part.delta`` event with the given delta string."""
    return {
        "type": "message.part.delta",
        "properties": {"field": field_name, "delta": delta},
    }


def _part_updated(
    part_id: str,
    *,
    tool: str = "bash",
    status: str = "running",
    title: str | None = None,
    input_: dict[str, Any] | None = None,
    error: str | None = None,
    part_type: str = "tool",
) -> dict[str, Any]:
    """Construct a ``message.part.updated`` event."""
    state: dict[str, Any] = {"status": status}
    if title is not None:
        state["title"] = title
    if input_ is not None:
        state["input"] = input_
    if error is not None:
        state["error"] = error
    return {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "id": part_id,
                "type": part_type,
                "tool": tool,
                "state": state,
            }
        },
    }


# ---------------------------------------------------------------------------
# Catch-all: unknown / malformed → None
# ---------------------------------------------------------------------------


def test_unknown_event_type_returns_none() -> None:
    """``server.connected`` and friends produce nothing."""
    state = EventMapperState()
    assert map_opencode_event({"type": "server.connected"}, state) is None


def test_missing_type_returns_none() -> None:
    """A dict with no ``type`` is silently dropped (defensive: bad bus
    framing shouldn't raise)."""
    state = EventMapperState()
    assert map_opencode_event({"properties": {}}, state) is None


def test_non_string_type_returns_none() -> None:
    """Type field present but not a string — also dropped silently."""
    state = EventMapperState()
    assert map_opencode_event({"type": 42}, state) is None


# ---------------------------------------------------------------------------
# message.part.delta → text event
# ---------------------------------------------------------------------------


def test_text_delta_becomes_text_event() -> None:
    """Happy path: streamed model token."""
    state = EventMapperState()
    ev = map_opencode_event(_delta_event("Hello"), state)
    assert ev == ThinkingEvent(
        kind="text",
        source=SCUFRIS_SOURCE,
        text="Hello",
        depth=0,
    )


def test_non_text_delta_field_ignored() -> None:
    """``reasoning`` deltas don't surface as user text. Same for
    ``tool.input`` and any other non-text field opencode might emit.
    """
    state = EventMapperState()
    assert (
        map_opencode_event(_delta_event("foo", field_name="reasoning"), state) is None
    )


def test_empty_text_delta_dropped() -> None:
    """Zero-length deltas would render as a no-op chunk; cleaner to
    skip them upstream."""
    state = EventMapperState()
    assert map_opencode_event(_delta_event(""), state) is None


def test_non_string_delta_dropped() -> None:
    """Defensive: delta isn't a string → silently drop."""
    state = EventMapperState()
    ev_dict = {
        "type": "message.part.delta",
        "properties": {"field": "text", "delta": 42},
    }
    assert map_opencode_event(ev_dict, state) is None


# ---------------------------------------------------------------------------
# extract_text_delta — parallel surface
# ---------------------------------------------------------------------------


def test_extract_text_delta_matches_mapper() -> None:
    """``extract_text_delta`` is the parallel surface for text-only
    accumulation. Same gating logic as the mapper's text branch.
    """
    assert extract_text_delta(_delta_event("Hi")) == "Hi"
    assert extract_text_delta(_delta_event("", field_name="reasoning")) is None
    assert extract_text_delta({"type": "other"}) is None


# ---------------------------------------------------------------------------
# message.part.updated — tool state machine
# ---------------------------------------------------------------------------


def test_tool_running_emits_tool_call() -> None:
    """First ``running`` update → exactly one ``tool_call`` event."""
    state = EventMapperState()
    ev = map_opencode_event(_part_updated("p1", tool="bash", title="ls -la"), state)
    assert ev == ThinkingEvent(
        kind="tool_call",
        source=SCUFRIS_SOURCE,
        text="bash",
        depth=0,
        arg="ls -la",
    )


def test_tool_running_dedup_drops_second_emission() -> None:
    """Subsequent ``running`` updates for the same part.id produce nothing."""
    state = EventMapperState()
    ev1 = map_opencode_event(_part_updated("p1", title="first"), state)
    ev2 = map_opencode_event(_part_updated("p1", title="second"), state)
    assert ev1 is not None
    assert ev2 is None


def test_tool_completed_emits_tool_result() -> None:
    """``completed`` after ``running`` produces exactly one ``tool_result``."""
    state = EventMapperState()
    map_opencode_event(_part_updated("p1", title="x", status="running"), state)
    ev = map_opencode_event(_part_updated("p1", status="completed"), state)
    assert ev == ThinkingEvent(
        kind="tool_result",
        source=SCUFRIS_SOURCE,
        text="bash",
        depth=0,
    )


def test_tool_completed_dedup_drops_second_emission() -> None:
    """A second ``completed`` for the same part.id is dropped."""
    state = EventMapperState()
    map_opencode_event(_part_updated("p1", status="running"), state)
    map_opencode_event(_part_updated("p1", status="completed"), state)
    ev = map_opencode_event(_part_updated("p1", status="completed"), state)
    assert ev is None


def test_tool_error_emits_tool_result_with_error_text() -> None:
    """``error`` status produces a ``tool_result`` whose ``text``
    field carries the failure message (capped)."""
    state = EventMapperState()
    map_opencode_event(_part_updated("p1", status="running"), state)
    ev = map_opencode_event(
        _part_updated("p1", status="error", error="permission denied"), state
    )
    assert ev is not None
    assert ev.kind == "tool_result"
    assert "permission denied" in ev.text
    assert "bash" in ev.text


def test_tool_pending_emits_nothing() -> None:
    """``pending`` is the pre-running idle state. No event."""
    state = EventMapperState()
    assert map_opencode_event(_part_updated("p1", status="pending"), state) is None


def test_tool_arg_falls_back_to_input_summary_when_no_title() -> None:
    """When ``state.title`` is empty, the mapper synthesises ``arg``
    from ``state.input`` via ``_summarise_input``."""
    state = EventMapperState()
    ev = map_opencode_event(
        _part_updated("p1", tool="grep", input_={"pattern": "foo"}), state
    )
    assert ev is not None
    assert ev.arg == "foo"


def test_tool_arg_truncated_at_cap() -> None:
    """Long titles get an ellipsis. Cap is 120 chars; the truncated
    arg surface ends with ``…``."""
    state = EventMapperState()
    long_title = "x" * 200
    ev = map_opencode_event(_part_updated("p1", title=long_title), state)
    assert ev is not None
    assert ev.arg is not None
    assert len(ev.arg) <= 120
    assert ev.arg.endswith("…")


def test_non_tool_part_ignored() -> None:
    """Text parts are surfaced via ``message.part.delta``, not
    ``message.part.updated``. Step-start, step-finish, patch, file,
    snapshot likewise produce nothing here."""
    state = EventMapperState()
    ev_dict = _part_updated("p1", part_type="text")
    assert map_opencode_event(ev_dict, state) is None


def test_running_event_marks_part_seen_for_subsequent_completion() -> None:
    """Even after a ``running``, the part.id is tracked so that
    a duplicate ``completed`` from a retry / replay still dedups."""
    state = EventMapperState()
    map_opencode_event(_part_updated("p1", status="running"), state)
    # First completion succeeds
    ev1 = map_opencode_event(_part_updated("p1", status="completed"), state)
    # Replayed completion suppressed
    ev2 = map_opencode_event(_part_updated("p1", status="completed"), state)
    assert ev1 is not None
    assert ev2 is None


# ---------------------------------------------------------------------------
# permission.updated → tool_meta
# ---------------------------------------------------------------------------


def test_permission_event_becomes_tool_meta() -> None:
    """Permission asks surface as ``tool_meta`` events with a
    ``permission: <title>`` text body. The full ask/decision UX
    lives in #30; this layer just surfaces visibility."""
    state = EventMapperState()
    ev = map_opencode_event(
        {
            "type": "permission.updated",
            "properties": {"id": "perm_1", "title": "run bash"},
        },
        state,
    )
    assert ev == ThinkingEvent(
        kind="tool_meta",
        source=SCUFRIS_SOURCE,
        text="permission: run bash",
        depth=0,
    )


def test_permission_event_falls_back_to_id_when_no_title() -> None:
    """If ``title`` is missing, the permission id is the next best label."""
    state = EventMapperState()
    ev = map_opencode_event(
        {"type": "permission.updated", "properties": {"id": "perm_2"}},
        state,
    )
    assert ev is not None
    assert ev.text == "permission: perm_2"


def test_permission_event_falls_back_to_default_label() -> None:
    """No title, no id → generic placeholder. The renderer should
    still surface that something needs attention."""
    state = EventMapperState()
    ev = map_opencode_event({"type": "permission.updated", "properties": {}}, state)
    assert ev is not None
    assert ev.text == "permission: permission required"


# ---------------------------------------------------------------------------
# Multi-call state isolation (one state per turn)
# ---------------------------------------------------------------------------


def test_fresh_state_per_turn() -> None:
    """A new turn's state machine doesn't inherit prior dedup data
    even for the same part.id. (Caller responsibility: allocate a
    fresh :class:`EventMapperState` per stream invocation.)
    """
    state1 = EventMapperState()
    ev1 = map_opencode_event(_part_updated("p1", status="running"), state1)
    state2 = EventMapperState()
    ev2 = map_opencode_event(_part_updated("p1", status="running"), state2)
    assert ev1 is not None
    assert ev2 is not None

"""Map raw OpenCode SSE events into :class:`ThinkingEvent` instances.

Pure-ish stateless mapping (with a small bookkeeping struct passed
across calls so we can dedup tool events by ``part.id``). Used by
:class:`utils.agent.AgentManager` to convert each event yielded by
:meth:`utils.opencode_client.OpenCodeClient.chat_stream` into the
SSE wire format the CLI / Telegram bot already understand.

See ``tasks/20260610-101413/SCHEMA.md`` for the full event taxonomy
and the mapping rationale.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from .callbacks import ThinkingEvent

# Single "agent" identity surfaced to the renderer. With OpenCode
# replacing the LangChain sub-agent hierarchy, every event lives at
# depth 0 with source ``"scufris"``. Renderers keep the depth field
# but always see depth=0 (no indentation).
SCUFRIS_SOURCE = "scufris"

# Cap on the human-friendly ``arg`` string surfaced to the trace
# renderer. Matches utils/callbacks.py's `truncate_log` budget for
# tool args.
_ARG_CHAR_CAP = 120

# Cap on tool_result error text. Generous: tool failures often paste
# stack traces, but we don't want to overflow the SSE frame either.
_RESULT_CHAR_CAP = 240


@dataclass
class EventMapperState:
    """Per-turn state carried across consecutive :func:`map_opencode_event` calls.

    Currently tracks tool-call dedup: a single tool produces multiple
    ``message.part.updated`` events as the state walks
    ``pending → running → … → completed | error``; we emit exactly
    one ``tool_call`` event per ``part.id`` (on the first
    ``running``) and exactly one ``tool_result`` per ``part.id`` (on
    ``completed`` or ``error``).
    """

    seen_tool_part_ids: Set[str] = field(default_factory=set)
    seen_tool_completion_ids: Set[str] = field(default_factory=set)


def extract_text_delta(raw: Dict[str, Any]) -> Optional[str]:
    """Return the delta string from a ``message.part.delta`` text event.

    ``None`` for any other event type or for non-text deltas. Used by
    :class:`AgentManager` to accumulate the final reply text in
    parallel with the trace mapper.
    """
    if raw.get("type") != "message.part.delta":
        return None
    props = raw.get("properties") or {}
    if props.get("field") != "text":
        return None
    delta = props.get("delta")
    if isinstance(delta, str):
        return delta
    return None


def map_opencode_event(
    raw: Dict[str, Any], state: EventMapperState
) -> Optional[ThinkingEvent]:
    """Map a raw OpenCode SSE event to a :class:`ThinkingEvent`.

    Returns ``None`` for events that have no user-facing surface (the
    majority of events: ``server.connected``, ``message.updated``,
    ``session.status``, ``session.next.*``, ``session.diff``,
    ``session.updated``, ``installation.*``, ``lsp.*``, ``storage.*``,
    ``file.watcher.*``, ``ide.*``, …). Mutates ``state`` to enforce
    one-shot semantics for tool events.

    Caller is responsible for filtering events by ``sessionID``
    upstream (see :class:`OpenCodeClient.chat_stream`) and for
    consuming text deltas separately via :func:`extract_text_delta`
    when reconstructing the final reply.

    See SCHEMA.md "Mapping → ThinkingEvent" for the full table.
    """
    etype = raw.get("type")
    if not isinstance(etype, str):
        return None
    props = raw.get("properties") or {}

    if etype == "message.part.delta":
        if props.get("field") != "text":
            return None
        delta = props.get("delta")
        if not isinstance(delta, str) or not delta:
            return None
        return ThinkingEvent(
            kind="text",
            source=SCUFRIS_SOURCE,
            text=delta,
            depth=0,
        )

    if etype == "message.part.updated":
        return _map_part_updated(props, state)

    if etype == "permission.updated":
        title = props.get("title") or props.get("id") or "permission required"
        return ThinkingEvent(
            kind="tool_meta",
            source=SCUFRIS_SOURCE,
            text=f"permission: {title}",
            depth=0,
        )

    return None


def _map_part_updated(
    props: Dict[str, Any], state: EventMapperState
) -> Optional[ThinkingEvent]:
    part = props.get("part")
    if not isinstance(part, dict):
        return None
    if part.get("type") != "tool":
        # Text parts are redundant with ``message.part.delta`` (we
        # already render those); step-start, step-finish, snapshot,
        # patch, file parts have no user-facing value at this layer.
        return None
    part_id = part.get("id")
    if not isinstance(part_id, str):
        return None
    tool_name = str(part.get("tool") or "unknown")
    pstate = part.get("state") or {}
    status = pstate.get("status")

    if status == "running":
        if part_id in state.seen_tool_part_ids:
            return None
        state.seen_tool_part_ids.add(part_id)
        title = pstate.get("title")
        if isinstance(title, str) and title.strip():
            arg: Optional[str] = title.strip()
        else:
            arg = _summarise_input(pstate.get("input"))
        if arg is not None and len(arg) > _ARG_CHAR_CAP:
            arg = arg[: _ARG_CHAR_CAP - 1] + "…"
        return ThinkingEvent(
            kind="tool_call",
            source=SCUFRIS_SOURCE,
            text=tool_name,
            depth=0,
            arg=arg,
        )

    if status == "completed":
        if part_id in state.seen_tool_completion_ids:
            return None
        state.seen_tool_completion_ids.add(part_id)
        # Suppress a second tool_call if the running event was
        # somehow missed (e.g. very fast tool).
        state.seen_tool_part_ids.add(part_id)
        return ThinkingEvent(
            kind="tool_result",
            source=SCUFRIS_SOURCE,
            text=tool_name,
            depth=0,
        )

    if status == "error":
        if part_id in state.seen_tool_completion_ids:
            return None
        state.seen_tool_completion_ids.add(part_id)
        state.seen_tool_part_ids.add(part_id)
        err = pstate.get("error") or pstate.get("message") or "unknown error"
        text = f"{tool_name} failed: {err}"
        if len(text) > _RESULT_CHAR_CAP:
            text = text[: _RESULT_CHAR_CAP - 1] + "…"
        return ThinkingEvent(
            kind="tool_result",
            source=SCUFRIS_SOURCE,
            text=text,
            depth=0,
        )

    # status == "pending" or unknown — emit nothing yet; the running
    # update is what triggers the tool_call event.
    return None


def _summarise_input(state_input: Any) -> Optional[str]:
    """Best-effort one-liner from a tool's input dict.

    Used when the tool's ``state.title`` is still empty (the first
    ``running`` update sometimes arrives before the title is
    populated). Looks for common semantic keys; falls back to the
    first scalar value; finally to a truncated JSON dump.
    """
    if state_input is None:
        return None
    if isinstance(state_input, str):
        return state_input.strip() or None
    if not isinstance(state_input, dict) or not state_input:
        return None
    for key in ("description", "command", "query", "path", "text", "pattern"):
        v = state_input.get(key)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    for v in state_input.values():
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    try:
        dumped = json.dumps(state_input, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return dumped if dumped else None

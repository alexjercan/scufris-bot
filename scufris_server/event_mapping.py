"""Map raw opencode SSE events into :class:`ThinkingEvent` instances.

Pure / stateless mapping (with a small bookkeeping struct passed
across calls so we can dedup tool events by ``part.id``). Used by
the streaming chat route (#11 step 8) to convert each event
yielded by the :class:`scufris_server.events.EventBus` into the
SSE wire format the v1 CLI renderer already understands.

This is a verbatim port of v1's ``utils/opencode_events.py``
(``feature/opencode`` branch) with two tweaks:

1. Import target is the new :mod:`scufris_server.events` location.
2. Type hints modernised to PEP 604 unions / lowercase generics
   (``dict[...]``, ``X | None``) for mypy --strict on Python
   3.13. Semantics identical.

The full event taxonomy and the mapping rationale live in v1's
``tasks/20260610-101413/SCHEMA.md``; the mapping table reproduced
here as inline comments. Three event types are interesting at
this layer; everything else (``server.connected``,
``message.updated``, ``session.status``, ``session.next.*``,
``session.diff``, ``session.updated``, ``installation.*``,
``lsp.*``, ``storage.*``, ``file.watcher.*``, ``ide.*``, ...)
maps to ``None`` and gets dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from scufris_server.events import SCUFRIS_SOURCE, ThinkingEvent

# Cap on the human-friendly ``arg`` string surfaced to the trace
# renderer. Matches v1 ``utils/callbacks.py``'s ``truncate_log``
# budget for tool args (120 chars + "…" suffix).
_ARG_CHAR_CAP: int = 120

# Cap on tool_result error text. Generous: tool failures often paste
# stack traces, but we don't want to overflow the SSE frame either.
_RESULT_CHAR_CAP: int = 240


# ---------------------------------------------------------------------------
# Per-turn dedup state
# ---------------------------------------------------------------------------


@dataclass
class EventMapperState:
    """Per-turn state carried across consecutive :func:`map_opencode_event` calls.

    Currently tracks tool-call dedup: a single tool produces multiple
    ``message.part.updated`` events as the state walks
    ``pending → running → … → completed | error``; we emit exactly
    one ``tool_call`` event per ``part.id`` (on the first
    ``running``) and exactly one ``tool_result`` per ``part.id`` (on
    ``completed`` or ``error``).

    The route handler instantiates one state per stream invocation;
    state is intentionally NOT shared across turns (a new turn means
    a new state machine). The handler discards the instance when
    the stream closes.
    """

    seen_tool_part_ids: set[str] = field(default_factory=set)
    seen_tool_completion_ids: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Text delta extraction (parallel surface — text accumulation)
# ---------------------------------------------------------------------------


def extract_text_delta(raw: dict[str, Any]) -> str | None:
    """Return the delta string from a ``message.part.delta`` text event.

    ``None`` for any other event type or for non-text deltas
    (opencode also emits deltas for ``reasoning``, ``tool.input``,
    etc. — those don't belong in the user-facing text reply).

    Used by the route handler to accumulate the final reply text in
    parallel with the trace mapper: the streaming endpoint emits
    ``thinking`` events via :func:`map_opencode_event` *and*
    independently builds the assembled reply text for the terminal
    ``done`` payload by appending each delta string. (As of #11 the
    handler instead awaits the synchronous POST's return value for
    the final text — opencode's ``send_message`` blocks until
    ``session.idle`` and returns the assembled message — so this
    helper isn't strictly required in the current flow. Kept
    available because the assembled-from-deltas path is a useful
    fallback if a future opencode version returns the POST
    asynchronously.)
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


# ---------------------------------------------------------------------------
# Main mapping entry point
# ---------------------------------------------------------------------------


def map_opencode_event(
    raw: dict[str, Any], state: EventMapperState
) -> ThinkingEvent | None:
    """Map a raw opencode SSE event to a :class:`ThinkingEvent`.

    Returns ``None`` for events that have no user-facing surface —
    the majority of opencode's event stream (``server.connected``,
    ``message.updated``, ``session.status``, ``session.next.*``,
    ``session.diff``, ``session.updated``, ``installation.*``,
    ``lsp.*``, ``storage.*``, ``file.watcher.*``, ``ide.*``, …).
    Mutates ``state`` to enforce one-shot semantics for tool events.

    The caller is responsible for filtering events by ``sessionID``
    upstream (the :class:`EventBus` dispatch layer does this; events
    leaking through carry the right sessionID by construction). The
    caller also consumes text deltas separately via
    :func:`extract_text_delta` when reconstructing the final reply.

    Mapping table (cribbed from v1 SCHEMA.md):

    | opencode event type     | ThinkingEvent kind | Notes                |
    |-------------------------|--------------------|----------------------|
    | message.part.delta      | text               | text field only      |
    | message.part.updated    | tool_call /        | tool parts only;     |
    |                         | tool_result        | dedup by part.id     |
    | permission.updated      | tool_meta          | ask + reply both     |
    | everything else         | None               | dropped              |
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


# ---------------------------------------------------------------------------
# message.part.updated — tool state machine
# ---------------------------------------------------------------------------


def _map_part_updated(
    props: dict[str, Any], state: EventMapperState
) -> ThinkingEvent | None:
    """Translate a ``message.part.updated`` properties dict to an event.

    Only ``part.type == "tool"`` parts produce events here. Text
    parts are redundant with ``message.part.delta`` (we already
    render those); ``step-start``, ``step-finish``, ``snapshot``,
    ``patch``, ``file`` parts have no user-facing value at this
    layer.

    State machine per ``part.id``:

    - ``pending``: emit nothing. The running update is what triggers
      the ``tool_call`` event.
    - ``running``: emit one ``tool_call`` per ``part.id`` (subsequent
      ``running`` updates for the same id are dropped via
      :attr:`EventMapperState.seen_tool_part_ids`).
    - ``completed``: emit one ``tool_result`` per ``part.id`` (subsequent
      completions dropped via :attr:`EventMapperState.seen_tool_completion_ids`).
    - ``error``: emit one ``tool_result`` per ``part.id`` with the
      error text (capped at :data:`_RESULT_CHAR_CAP`).

    If a tool is so fast that ``running`` was missed (rare; we've
    never observed it but the dedup logic guards against double-
    emit), the completion / error path still marks the part as seen
    to suppress a stale ``tool_call``.
    """
    part = props.get("part")
    if not isinstance(part, dict):
        return None
    if part.get("type") != "tool":
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
            arg: str | None = title.strip()
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

    # status == "pending" or unknown — emit nothing yet.
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summarise_input(state_input: Any) -> str | None:
    """Best-effort one-liner from a tool's input dict.

    Used when the tool's ``state.title`` is still empty (the first
    ``running`` update sometimes arrives before the title is
    populated). Resolution order:

    1. ``None`` if input is None or empty.
    2. The stripped string itself if input is already a string.
    3. The value of the first semantically-meaningful key
       (``description``, ``command``, ``query``, ``path``,
       ``text``, ``pattern``) that has a non-empty scalar.
    4. The first scalar value found, if any.
    5. A JSON dump as last resort.

    All resolution paths return a non-empty stripped string, or
    ``None`` if nothing usable was found.
    """
    if state_input is None:
        return None
    if isinstance(state_input, str):
        return state_input.strip() or None
    if not isinstance(state_input, dict) or not state_input:
        return None
    for key in ("description", "command", "query", "path", "text", "pattern"):
        v = state_input.get(key)
        if isinstance(v, str | int | float) and str(v).strip():
            return str(v).strip()
    for v in state_input.values():
        if isinstance(v, str | int | float) and str(v).strip():
            return str(v).strip()
    try:
        dumped = json.dumps(state_input, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return dumped if dumped else None

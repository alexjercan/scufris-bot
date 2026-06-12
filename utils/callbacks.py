"""Thinking-trail event types + name parsing helpers.

The Scufris CLI / Telegram bot used to host a depth-aware
``ToolCallbackHandler`` here that hooked into LangChain's callback
system. After the OpenCode runtime swap (``tasks/20260610-101413``)
LangChain is gone, so the handler went with it: the runtime now
emits :class:`ThinkingEvent` instances directly while consuming
OpenCode's SSE event stream.

What stayed is the small set of pure helpers / data classes that
the rest of the codebase still depends on:

- :data:`DISPLAY_NAMES` / :data:`SUB_AGENT_NAMES` — display tables
  used by both the CLI renderer and the OpenCode listener.
- :func:`display_name`, :func:`is_sub_agent` — name lookups.
- :func:`_parse_tool_arg`, :func:`_parse_tool_context` — best-effort
  arg / context extraction from a stringified tool input.
- :class:`ThinkingEvent` — the user-visible event type emitted into
  the CLI thinking trail.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Literal, Optional

# Display names used to render technical tool/agent identifiers in the
# user-facing thinking trail. Falls back to Title Case if missing.
DISPLAY_NAMES: Dict[str, str] = {
    "main": "Scufris",
    "coding_agent": "Coding Agent",
    "knowledge_agent": "Knowledge Agent",
    "utilities_agent": "Utilities Agent",
    "journal_agent": "Journal Agent",
    "weather": "Weather",
    "web_search": "Web Search",
    "calculator_tool": "Calculator",
    "datetime_tool": "Date/Time",
    "opencode": "OpenCode",
}


# Tool names that are themselves agents (delegations look like "asks"
# rather than "uses"). Anything ending in "_agent" is also treated as
# a sub-agent.
SUB_AGENT_NAMES = {
    "coding_agent",
    "knowledge_agent",
    "utilities_agent",
    "journal_agent",
}


def display_name(technical: str) -> str:
    """Map a technical tool/agent name to a human-friendly display name."""
    if technical in DISPLAY_NAMES:
        return DISPLAY_NAMES[technical]
    return technical.replace("_", " ").title()


def is_sub_agent(name: str) -> bool:
    return name in SUB_AGENT_NAMES or name.endswith("_agent")


def _parse_tool_arg(input_str: str) -> Optional[str]:
    """Extract a single human-meaningful argument from a tool input string.

    LangChain tool inputs typically arrive as JSON like
    ``{"query": "weather in Bucharest"}`` or ``{"__arg1": "Bucharest"}``.
    We try to pull out the first scalar value so the CLI can render
    "→ ... Knowledge Agent: weather in Bucharest" instead of dumping
    the whole dict. Returns ``None`` if there's nothing worth showing.
    """
    s = input_str.strip()
    if not s:
        return None
    parsed: Any = None
    try:
        parsed = json.loads(s)
    except (ValueError, TypeError):
        # LangChain sometimes hands us Python repr (single quotes) instead
        # of JSON, e.g. "{'__arg1': 'Ploiesti'}". Try literal_eval as a
        # fallback before giving up and using the raw string.
        try:
            parsed = ast.literal_eval(s)
        except (ValueError, SyntaxError):
            return s
    if isinstance(parsed, str):
        return parsed
    if isinstance(parsed, dict) and parsed:
        # Prefer common semantic keys; otherwise take the first scalar.
        for key in ("query", "expression", "input", "text", "__arg1"):
            if key in parsed and isinstance(parsed[key], (str, int, float)):
                return str(parsed[key])
        for value in parsed.values():
            if isinstance(value, (str, int, float)):
                return str(value)
    return s


def _parse_tool_context(input_str: str) -> Optional[str]:
    """Extract the Phase-2 ``context`` field from a sub-agent tool input.

    Returns the context string when the tool input is a dict containing a
    non-empty ``context`` key (i.e. a sub-agent call). Returns ``None`` for
    everything else — leaf tools, dicts without ``context``, or unparseable
    input. Empty strings are treated as "no context" so the trace stays
    quiet for cold-start delegations.
    """
    s = input_str.strip()
    if not s:
        return None
    try:
        parsed = json.loads(s)
    except (ValueError, TypeError):
        try:
            parsed = ast.literal_eval(s)
        except (ValueError, SyntaxError):
            return None
    if not isinstance(parsed, dict):
        return None
    ctx = parsed.get("context")
    if isinstance(ctx, str) and ctx.strip():
        return ctx
    return None


@dataclass
class ThinkingEvent:
    """A user-visible "thinking" event surfaced into the CLI trail.

    Used to live alongside the LangChain ``ToolCallbackHandler``;
    after the OpenCode swap the runtime emits these directly while
    consuming OpenCode's SSE event stream. The CLI renders them as
    dim chat-style messages above the final assistant reply. The
    Telegram bot ignores them by default.
    """

    kind: Literal["text", "tool_call", "tool_result", "tool_meta", "compaction"]
    source: str  # e.g. "main", "knowledge_agent" (raw technical name)
    text: str  # for tool_call: target tool name; for text: the message
    depth: int  # nesting level (for indentation/styling)
    arg: Optional[str] = None  # human-meaningful argument, if any
    context: Optional[str] = None  # Phase-2 sub-agent `context` field, if any
    # Phase 3.5 — for `tool_meta` events emitted in `on_tool_end`,
    # the count of prior history turns the sub-agent loaded for THIS
    # call (>0 only). The CLI renders it as `↳ +N prior turns`.
    prior_turns: Optional[int] = None
    # Phase 3 — for `compaction` events: how many messages were
    # evicted in the salvage and how many new facts were extracted.
    # Both >0 (the history manager only emits when something was
    # actually salvaged).
    evicted: Optional[int] = None
    new_facts: Optional[int] = None


# Type alias for the on_thinking callback.
ThinkingCallback = Callable[[ThinkingEvent], None]

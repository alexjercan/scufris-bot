"""Per-agent memory tools: ``remember`` and ``forget`` (Phase 3).

Each agent that keeps history gets its own pair of tools, built by
:func:`make_memory_tools`. The factory captures the
``history_manager`` + ``agent_name`` in a closure so the tool body can
route writes to the correct ``(user_id, agent_name)`` slice without
the caller having to thread the agent name through every invocation.

Validation is intentionally lenient: bad input returns a readable
error string rather than raising, so the LLM can recover from the
failure inside the same turn (instead of the agent loop crashing).
"""

from __future__ import annotations

from typing import List, Optional

from langchain.tools import tool
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from ..history import ChatHistoryManager

# Per Phase 3 spec ACs.
_MAX_KEY_CHARS = 32
_MAX_VALUE_CHARS = 200


def _resolve_user_id(config: Optional[RunnableConfig]) -> Optional[int]:
    """Pull ``user_id`` out of ``config["configurable"]``.

    Returns ``None`` when missing so the tool body can return a
    readable error rather than blowing up the agent loop.
    """
    if not config:
        return None
    configurable = config.get("configurable") or {}
    uid = configurable.get("user_id")
    return uid if isinstance(uid, int) else None


def make_memory_tools(
    history_manager: ChatHistoryManager,
    agent_name: str,
) -> List[BaseTool]:
    """Build a fresh pair of ``[remember, forget]`` tools for an agent.

    The closures capture ``history_manager`` and ``agent_name`` so the
    tools route to the correct slice. Each call resolves ``user_id``
    from the injected :class:`RunnableConfig` (set by ``AgentManager``
    on the outer invocation and inherited by every nested run).
    """

    @tool
    def remember(key: str, value: str, config: RunnableConfig) -> str:
        """Store a durable fact about the user (slot-keyed, last-write-wins).

        Use this when the user tells you something worth remembering
        across future turns (location, preferences, timezone, ongoing
        project, etc.). The fact is scoped to *this* agent — other
        sub-agents have their own fact stores.

        Args:
            key: Short slot name, e.g. ``"location"``. ≤32 chars,
                non-empty. Reusing a key overwrites the previous value.
            value: The fact, e.g. ``"Bucharest"``. ≤200 chars,
                non-empty.

        Returns:
            Confirmation string, or a readable error message when
            input is invalid or ``user_id`` is missing.
        """
        if not key or not key.strip():
            return "error: key must be non-empty"
        if len(key) > _MAX_KEY_CHARS:
            return f"error: key too long ({len(key)}ch, max {_MAX_KEY_CHARS})"
        if not value or not value.strip():
            return "error: value must be non-empty"
        if len(value) > _MAX_VALUE_CHARS:
            return f"error: value too long ({len(value)}ch, max {_MAX_VALUE_CHARS})"
        user_id = _resolve_user_id(config)
        if user_id is None:
            return "error: user_id missing from invocation config"
        history_manager.add_facts(user_id, agent_name, {key: value}, source="remember")
        return f"remembered: {key} = {value}"

    @tool
    def forget(key: str, config: RunnableConfig) -> str:
        """Drop a single fact from this agent's memory by key.

        Call this when the user retracts a fact or asks you to forget
        something. Do **not** silently stop using a fact — call
        ``forget`` so the durable store agrees with the user.

        Args:
            key: The slot name previously passed to ``remember``.

        Returns:
            ``"forgot: <key>"`` on success,
            ``"no such fact: <key>"`` when the key was not present,
            or a readable error string for bad input.
        """
        if not key or not key.strip():
            return "error: key must be non-empty"
        user_id = _resolve_user_id(config)
        if user_id is None:
            return "error: user_id missing from invocation config"
        removed = history_manager.remove_fact(user_id, agent_name, key)
        return f"forgot: {key}" if removed else f"no such fact: {key}"

    return [remember, forget]

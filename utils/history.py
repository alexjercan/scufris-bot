"""Chat history management for the Scufris Bot."""

import logging
from collections import defaultdict
from typing import Any, Dict, List, Tuple

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

# Default agent slot — the main user-facing agent.
# Sub-agents use their own name (e.g. "knowledge_agent").
SCUFRIS_AGENT = "scufris"

# Char-to-token proxy ratio for budget trimming. Qwen-ish; deliberately
# coarse — Phase 4 will tune the budgets, not the ratio.
_CHARS_PER_TOKEN = 4


class ChatHistoryManager:
    """Manages chat history for multiple users and agents.

    The store is keyed by ``(user_id, agent_name)``. The main agent uses
    ``agent="scufris"`` (the default for legacy callsites). Sub-agents
    that opt into per-agent memory pass their own name.
    """

    def __init__(self, max_history_per_user: int = 20):
        """
        Initialize the chat history manager.

        Args:
            max_history_per_user: Maximum number of messages to keep in
                the *main* (scufris) slice per user. Sub-agent slices
                use a token-budget trim instead — see ``add_messages``.
        """
        self.logger = logging.getLogger("scufris-bot.history")
        self.max_history_per_user = max_history_per_user

        # Dictionary keyed by (user_id, agent_name).
        self._histories: Dict[Tuple[int, str], List[BaseMessage]] = defaultdict(list)

        self.logger.info(
            f"Initialized chat history manager (max {max_history_per_user} messages per user, main flow)"
        )

    # ------------------------------------------------------------------
    # Main-flow API (backward compatible: agent defaults to "scufris")
    # ------------------------------------------------------------------

    def add_user_message(
        self, user_id: int, message: str, agent: str = SCUFRIS_AGENT
    ) -> None:
        """Add a user message to the history."""
        key = (user_id, agent)
        self._histories[key].append(HumanMessage(content=message))
        self._trim_history(key)
        self.logger.debug(f"Added user message for user {user_id} (agent {agent})")

    def add_ai_message(
        self, user_id: int, message: str, agent: str = SCUFRIS_AGENT
    ) -> None:
        """Add an AI message to the history."""
        key = (user_id, agent)
        self._histories[key].append(AIMessage(content=message))
        self._trim_history(key)
        self.logger.debug(f"Added AI message for user {user_id} (agent {agent})")

    def get_history(
        self, user_id: int, agent: str = SCUFRIS_AGENT
    ) -> List[BaseMessage]:
        """Get the chat history for a (user, agent) slice."""
        return list(self._histories.get((user_id, agent), []))

    def get_history_with_new_message(
        self, user_id: int, new_message: str, agent: str = SCUFRIS_AGENT
    ) -> List[Dict[str, str]]:
        """Get history with a new user message appended (dict format for agent input)."""
        history = self.get_history(user_id, agent=agent)
        messages = [
            {
                "role": "user" if isinstance(msg, HumanMessage) else "assistant",
                "content": msg.content,
            }
            for msg in history
        ]
        messages.append({"role": "user", "content": new_message})
        return messages

    def get_message_count(self, user_id: int, agent: str = SCUFRIS_AGENT) -> int:
        """Get the number of messages in a (user, agent) slice."""
        return len(self._histories.get((user_id, agent), []))

    # ------------------------------------------------------------------
    # Sub-agent API (raw BaseMessage append + token-budget trim)
    # ------------------------------------------------------------------

    def add_messages(
        self,
        user_id: int,
        agent: str,
        messages: List[BaseMessage],
        token_budget: int,
    ) -> None:
        """Append raw messages to a sub-agent slice and trim to ``token_budget``.

        Used by sub-agents that opt into per-agent memory. Stores the
        raw ``BaseMessage`` instances (preserving tool-call structure,
        not just string content). Trim is char-proxy based — see
        ``_trim_by_tokens``.

        Args:
            user_id: User ID
            agent: Sub-agent name (e.g. ``"knowledge_agent"``)
            messages: New messages to append
            token_budget: Soft cap on the slice's char-proxy token count
        """
        if not messages:
            return
        key = (user_id, agent)
        self._histories[key].extend(messages)
        removed = self._trim_by_tokens(key, token_budget)
        self.logger.debug(
            f"Appended {len(messages)} message(s) for user {user_id} (agent {agent}); "
            f"trimmed {removed} oldest message(s) to fit ~{token_budget} tokens"
        )

    # ------------------------------------------------------------------
    # Clearing
    # ------------------------------------------------------------------

    def clear_user(self, user_id: int) -> int:
        """Clear every (user, agent) slice for the given user.

        Returns:
            Total number of messages removed across all slices.
        """
        keys_to_drop = [k for k in self._histories if k[0] == user_id]
        total = 0
        for key in keys_to_drop:
            total += len(self._histories[key])
            del self._histories[key]
        if total:
            self.logger.info(
                f"Cleared {total} message(s) across {len(keys_to_drop)} slice(s) for user {user_id}"
            )
        return total

    def clear_history(self, user_id: int) -> int:
        """Backwards-compatible alias for :meth:`clear_user`."""
        return self.clear_user(user_id)

    def get_user_breakdown(self, user_id: int) -> Dict[str, int]:
        """Return per-agent message counts for the given user.

        Only includes agents with non-empty slices. Used by ``/clear``
        and ``/stats`` to render a per-agent breakdown without leaking
        the internal ``_histories`` dict.
        """
        return {
            agent: len(msgs)
            for (uid, agent), msgs in self._histories.items()
            if uid == user_id and msgs
        }

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_user_count(self) -> int:
        """Number of distinct users with any history."""
        return len({user_id for (user_id, _agent) in self._histories})

    def get_stats(self) -> Dict[str, Any]:
        """Return aggregate stats including per-agent breakdown."""
        total_messages = sum(len(h) for h in self._histories.values())
        per_agent: Dict[str, int] = defaultdict(int)
        for (_user_id, agent), msgs in self._histories.items():
            per_agent[agent] += len(msgs)
        return {
            "total_users": self.get_user_count(),
            "total_messages": total_messages,
            "max_history_per_user": self.max_history_per_user,
            "messages_per_agent": dict(per_agent),
        }

    # ------------------------------------------------------------------
    # Internal trim helpers
    # ------------------------------------------------------------------

    def _trim_history(self, key: Tuple[int, str]) -> None:
        """Trim a slice to ``max_history_per_user`` (main-flow message-count cap)."""
        history = self._histories[key]
        if len(history) > self.max_history_per_user:
            removed_count = len(history) - self.max_history_per_user
            self._histories[key] = history[-self.max_history_per_user :]
            self.logger.debug(
                f"Trimmed {removed_count} old message(s) from slice {key}"
            )

    def _trim_by_tokens(self, key: Tuple[int, str], token_budget: int) -> int:
        """Pop oldest messages until char-proxy total fits ``token_budget``.

        Preserves message boundaries (never splits a message). Returns
        the number of messages removed.
        """
        history = self._histories[key]
        char_budget = max(0, token_budget) * _CHARS_PER_TOKEN
        total_chars = sum(len(str(m.content)) for m in history)
        removed = 0
        # Always keep at least one message — better to slightly exceed
        # the budget than ship an empty history slice that defeats the
        # whole point of the sub-agent's memory.
        while total_chars > char_budget and len(history) > 1:
            evicted = history.pop(0)
            total_chars -= len(str(evicted.content))
            removed += 1
        return removed


def create_history_manager(max_history_per_user: int = 20) -> ChatHistoryManager:
    """Create and return a chat history manager instance."""
    return ChatHistoryManager(max_history_per_user)

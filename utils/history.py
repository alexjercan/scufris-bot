"""Chat history management for the Scufris Bot."""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from .callbacks import ThinkingEvent
from .memory_compactor import (
    Compactor,
    FactEntry,
    FactSource,
    NoopCompactor,
    format_age,
    make_fact_entry,
)

# Default agent slot — the main user-facing agent.
# Sub-agents use their own name (e.g. "knowledge_agent").
SCUFRIS_AGENT = "scufris"

# Char-to-token proxy ratio for budget trimming. Qwen-ish; deliberately
# coarse — Phase 4 will tune the budgets, not the ratio.
_CHARS_PER_TOKEN = 4

# Hard caps on the summary + facts layers (per (user, agent) slice).
# Phase 1 of the history-compaction rollout. See
# tasks/20260510-183121/TASK.md.
_SUMMARY_CHAR_CAP = 1500
_FACTS_ENTRY_CAP = 20


class ChatHistoryManager:
    """Manages chat history for multiple users and agents.

    The store is keyed by ``(user_id, agent_name)``. The main agent uses
    ``agent="scufris"`` (the default for legacy callsites). Sub-agents
    that opt into per-agent memory pass their own name.
    """

    def __init__(
        self,
        max_history_per_user: int = 20,
        compactor: Optional[Compactor] = None,
        event_sink: Optional[Callable[[ThinkingEvent], None]] = None,
    ):
        """
        Initialize the chat history manager.

        Args:
            max_history_per_user: Maximum number of messages to keep in
                the *main* (scufris) slice per user. Sub-agent slices
                use a token-budget trim instead — see ``add_messages``.
            compactor: Strategy invoked on eviction to salvage
                summary + facts from to-be-dropped messages. Defaults
                to :class:`NoopCompactor` (no-op, behaviour-preserving).
                Phase 2 swaps in :class:`LLMCompactor` at bootstrap.
            event_sink: Optional callable invoked with a
                :class:`ThinkingEvent` whenever compaction salvages
                something. Wired by the CLI/Telegram bootstrap so the
                user-visible thinking trail can show ``[memory] ...``
                lines (Phase 3). Defaults to a no-op. Use
                :meth:`set_event_sink` to install one after
                construction (the manager is created before the
                callback handler in current bootstrap order).
        """
        self.logger = logging.getLogger("scufris-bot.history")
        self.max_history_per_user = max_history_per_user
        self._compactor: Compactor = compactor or NoopCompactor()
        self._event_sink: Optional[Callable[[ThinkingEvent], None]] = event_sink

        # Dictionary keyed by (user_id, agent_name).
        self._histories: Dict[Tuple[int, str], List[BaseMessage]] = defaultdict(list)

        # Compaction layers (Phase 2: populated by the compactor on
        # eviction and consumed by prompt assembly). Keyed identically
        # to _histories. Facts carry per-entry provenance metadata
        # (:class:`FactEntry`).
        self._summaries: Dict[Tuple[int, str], str] = defaultdict(str)
        self._facts: Dict[Tuple[int, str], Dict[str, FactEntry]] = defaultdict(dict)

        # Per-agent registry (set by create_sub_agent at build time).
        # Keyed by agent name; values describe the agent's memory config.
        self._agent_registry: Dict[str, Dict[str, Any]] = {}

        # Per-(user, agent) telemetry — survives /clear (counters are
        # about call traffic, not memory contents).
        self._invocations: Dict[Tuple[int, str], int] = defaultdict(int)
        self._last_activity: Dict[Tuple[int, str], datetime] = {}

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
    ) -> List[Dict[str, Any]]:
        """Get history with a new user message appended (dict format for agent input).

        Phase 2: prepends a system message exposing the running
        summary and another exposing known facts (with provenance +
        age) when either is non-empty. Order is
        ``facts → summary → window → new_message`` so durable
        context lives closest to the system prompt where it has the
        most influence on the model.
        """
        history = self.get_history(user_id, agent=agent)
        messages: List[Dict[str, Any]] = []
        for ctx in self._build_context_messages(user_id, agent):
            messages.append({"role": "system", "content": ctx.content})
        for msg in history:
            messages.append(
                {
                    "role": "user" if isinstance(msg, HumanMessage) else "assistant",
                    "content": msg.content,
                }
            )
        messages.append({"role": "user", "content": new_message})
        return messages

    def build_context_messages(
        self, user_id: int, agent: str = SCUFRIS_AGENT
    ) -> List[BaseMessage]:
        """Public wrapper around :meth:`_build_context_messages`.

        Returns a list of :class:`SystemMessage` instances (possibly
        empty) suitable for prepending to a sub-agent's
        ``input_messages``. Used by ``agent_builder.sub_agent_tool``
        so sub-agents see their own slice's facts + summary.
        """
        return list(self._build_context_messages(user_id, agent))

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

        Wipes message history, summaries and facts. Telemetry
        counters survive (they're about traffic, not content).

        Returns:
            Total number of messages removed across all slices.
        """
        keys_to_drop = [k for k in self._histories if k[0] == user_id]
        total = 0
        for key in keys_to_drop:
            total += len(self._histories[key])
            del self._histories[key]
        # Also wipe compaction layers for this user.
        for key in [k for k in self._summaries if k[0] == user_id]:
            del self._summaries[key]
        for key in [k for k in self._facts if k[0] == user_id]:
            del self._facts[key]
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

    def get_token_estimate(self, user_id: int, agent: str = SCUFRIS_AGENT) -> int:
        """Char-proxy token estimate for a (user, agent) slice."""
        msgs = self._histories.get((user_id, agent), [])
        chars = sum(len(str(m.content)) for m in msgs)
        return chars // _CHARS_PER_TOKEN

    def register_agent(
        self,
        agent: str,
        token_budget: Optional[int] = None,
        history_disabled: bool = False,
        model: Optional[str] = None,
    ) -> None:
        """Register an agent's memory + model config for stats reporting.

        Called from ``create_sub_agent`` (and ``setup_scufris`` for the
        main agent) so ``/stats`` can render budget, utilization and
        model columns without hard-coding the constants. Idempotent —
        re-registering replaces the entry.

        Args:
            agent: Agent name (e.g. ``"knowledge_agent"`` or
                ``"scufris"`` for the main agent).
            token_budget: Soft cap for the slice (``None`` when history
                is disabled or the agent uses a message-count cap).
            history_disabled: ``True`` for agents that don't keep
                history (e.g. ``utilities_agent``).
            model: Ollama model identifier, for the /stats model
                column. ``None`` when unknown.
        """
        self._agent_registry[agent] = {
            "token_budget": token_budget,
            "history_disabled": history_disabled,
            "model": model,
        }

    def set_event_sink(self, sink: Optional[Callable[[ThinkingEvent], None]]) -> None:
        """Install (or clear) the post-compaction event sink.

        Bootstrap order in ``main.py`` / ``cli.py`` creates the
        history manager *before* the ``ToolCallbackHandler``, so the
        sink can't be passed to ``__init__``. Use this setter from
        the bootstrap site once both are in scope.
        """
        self._event_sink = sink

    def record_invocation(self, user_id: int, agent: str) -> None:
        """Increment invocation counter and update last-activity timestamp.

        Called from ``sub_agent_tool`` on every call, regardless of
        whether the agent keeps history.
        """
        key = (user_id, agent)
        self._invocations[key] += 1
        self._last_activity[key] = datetime.now(timezone.utc)

    def get_user_telemetry(self, user_id: int) -> Dict[str, Dict[str, Any]]:
        """Return per-agent telemetry for the given user.

        Result schema (one entry per agent ever invoked OR registered):
            {
                "knowledge_agent": {
                    "messages": int,
                    "tokens": int,
                    "budget": Optional[int],
                    "history_disabled": bool,
                    "invocations": int,
                    "last_activity": Optional[datetime],
                },
                ...
            }
        """
        agents: set[str] = set(self._agent_registry.keys())
        for uid, agent in self._histories:
            if uid == user_id:
                agents.add(agent)
        for uid, agent in self._invocations:
            if uid == user_id:
                agents.add(agent)

        out: Dict[str, Dict[str, Any]] = {}
        for agent in agents:
            reg = self._agent_registry.get(agent, {})
            out[agent] = {
                "messages": len(self._histories.get((user_id, agent), [])),
                "tokens": self.get_token_estimate(user_id, agent),
                "budget": reg.get("token_budget"),
                "history_disabled": reg.get("history_disabled", False),
                "model": reg.get("model"),
                "invocations": self._invocations.get((user_id, agent), 0),
                "last_activity": self._last_activity.get((user_id, agent)),
                # Phase 3: compaction-layer visibility for /stats.
                "summary_chars": len(self._summaries.get((user_id, agent), "")),
                "facts_count": len(self._facts.get((user_id, agent), {})),
            }
        return out

    def get_stats(self) -> Dict[str, Any]:
        """Return aggregate stats including per-agent breakdown."""
        total_messages = sum(len(h) for h in self._histories.values())
        per_agent: Dict[str, int] = defaultdict(int)
        for (_user_id, agent), msgs in self._histories.items():
            per_agent[agent] += len(msgs)
        total_invocations = sum(self._invocations.values())
        return {
            "total_users": self.get_user_count(),
            "total_messages": total_messages,
            "max_history_per_user": self.max_history_per_user,
            "messages_per_agent": dict(per_agent),
            "total_invocations": total_invocations,
        }

    # ------------------------------------------------------------------
    # Compaction layer accessors (Phase 1: storage only — populated
    # solely by the compactor on eviction; consumed by Phase 2's
    # prompt assembly and Phase 3's remember/forget tools).
    # ------------------------------------------------------------------

    def get_summary(self, user_id: int, agent: str = SCUFRIS_AGENT) -> str:
        """Return the running summary for a (user, agent) slice.

        Empty string when nothing has been compacted yet.
        """
        return self._summaries.get((user_id, agent), "")

    def get_facts(self, user_id: int, agent: str = SCUFRIS_AGENT) -> Dict[str, str]:
        """Return a value-only copy of the facts hashmap for a slice.

        Backward-compatible with Phase 1 callers that just want
        ``key → value`` strings. For provenance metadata (source +
        timestamp) use :meth:`get_facts_with_meta`.
        """
        slot = self._facts.get((user_id, agent), {})
        return {k: e.value for k, e in slot.items()}

    def get_facts_with_meta(
        self, user_id: int, agent: str = SCUFRIS_AGENT
    ) -> Dict[str, FactEntry]:
        """Return a copy of the facts hashmap with full provenance.

        Each value is a :class:`FactEntry` carrying ``value``,
        ``source`` (``"compactor"`` | ``"remember"``) and ``timestamp``
        (unix epoch). Returned dict is a shallow copy; mutations
        don't leak back into the manager.
        """
        return dict(self._facts.get((user_id, agent), {}))

    def add_facts(
        self,
        user_id: int,
        agent: str,
        facts: Dict[str, str],
        source: FactSource = "remember",
    ) -> None:
        """Merge facts into a slice (last-write-wins on key collision).

        Each fact is wrapped in a :class:`FactEntry` carrying the
        provided ``source`` and the current timestamp. Drops oldest
        entries (FIFO insertion order) if the slice exceeds
        ``_FACTS_ENTRY_CAP`` after merge.

        ``source`` defaults to ``"remember"`` because the public API
        is primarily used by the (Phase 3) ``remember`` tool. The
        compactor wiring in :meth:`_run_compactor` passes
        ``source="compactor"`` explicitly.
        """
        if not facts:
            return
        key = (user_id, agent)
        slot = self._facts[key]
        for k, v in facts.items():
            # Last-write-wins: re-insert to refresh insertion order.
            if k in slot:
                del slot[k]
            slot[k] = make_fact_entry(v, source)
        # Cap: drop oldest until under the limit.
        overflow = len(slot) - _FACTS_ENTRY_CAP
        if overflow > 0:
            for old_key in list(slot.keys())[:overflow]:
                del slot[old_key]

    def remove_fact(self, user_id: int, agent: str, key_to_remove: str) -> bool:
        """Remove a single fact key from a slice.

        Returns ``True`` if the key existed and was removed,
        ``False`` if no such key was present.
        """
        slot = self._facts.get((user_id, agent))
        if slot and key_to_remove in slot:
            del slot[key_to_remove]
            return True
        return False

    # ------------------------------------------------------------------
    # Internal trim helpers
    # ------------------------------------------------------------------

    def _run_compactor(self, key: Tuple[int, str], evicted: List[BaseMessage]) -> None:
        """Hand evicted messages to the compactor and merge results.

        Errors are caught + logged: the window is the source of
        truth, so eviction must always succeed even if the
        compactor blows up.
        """
        if not evicted:
            return
        existing_summary = self._summaries.get(key, "")
        # Compactor sees a value-only view (no provenance). Wrapping
        # with source="compactor" happens on merge below.
        existing_facts_view = {k: e.value for k, e in self._facts.get(key, {}).items()}
        try:
            result = self._compactor.compact(
                evicted, existing_summary, existing_facts_view
            )
        except Exception:
            self.logger.warning(
                f"Compactor raised on slice {key}; eviction proceeds without "
                "salvaging summary/facts",
                exc_info=True,
            )
            return
        # Merge summary (clip to cap; ellipsis on overflow).
        new_summary = result.get("summary", existing_summary) or ""
        if len(new_summary) > _SUMMARY_CHAR_CAP:
            new_summary = new_summary[: _SUMMARY_CHAR_CAP - 1] + "…"
        if new_summary:
            self._summaries[key] = new_summary
        # Merge facts via add_facts (handles cap + last-write-wins).
        new_facts = result.get("facts") or {}
        if new_facts:
            self.add_facts(key[0], key[1], new_facts, source="compactor")
        # Telemetry hook: log a single line so the CLI / file log
        # shows when compaction actually salvaged something. Phase 3
        # will upgrade this to a structured ThinkingEvent.compaction.
        if new_summary or new_facts:
            self.logger.info(
                "[memory] %s: compacted %d message(s), summary=%dch, +%d fact(s)",
                key[1],
                len(evicted),
                len(new_summary),
                len(new_facts),
            )
            # Phase 3: surface as a structured thinking-trail event so
            # the CLI can render `[memory] knowledge_agent: compacted
            # N msg(s), +K fact(s)`. Only emitted when the salvage
            # actually produced something — empty compactions stay
            # silent.
            if self._event_sink is not None:
                try:
                    self._event_sink(
                        ThinkingEvent(
                            kind="compaction",
                            source=key[1],
                            text=key[1],
                            depth=0,
                            evicted=len(evicted),
                            new_facts=len(new_facts),
                        )
                    )
                except Exception:  # pragma: no cover — never break eviction
                    self.logger.exception("event_sink raised on compaction event")

    def _build_context_messages(self, user_id: int, agent: str) -> List[BaseMessage]:
        """Construct the system messages prepended to a slice's prompt.

        Returns ``[]`` when both summary and facts are empty so the
        prompt shape is identical to pre-Phase-2 in cold-start state.
        Otherwise returns up to two :class:`SystemMessage` instances
        in the documented order: facts first, then summary.

        Facts render as ``- key: value (source, age)`` lines so the
        agent (and the human reading the trace) can see *where* a
        fact came from. This addresses the user's request for
        provenance in the logs.
        """
        out: List[BaseMessage] = []
        facts = self._facts.get((user_id, agent), {})
        if facts:
            lines = ["Known facts about the user (slot-keyed, durable):"]
            for k, entry in facts.items():
                lines.append(
                    f"- {k}: {entry.value} ({entry.source}, "
                    f"{format_age(entry.timestamp)})"
                )
            out.append(SystemMessage(content="\n".join(lines)))
        summary = self._summaries.get((user_id, agent), "")
        if summary:
            out.append(
                SystemMessage(content=f"Earlier conversation summary: {summary}")
            )
        return out

    def _trim_history(self, key: Tuple[int, str]) -> None:
        """Trim a slice to ``max_history_per_user`` (main-flow message-count cap)."""
        history = self._histories[key]
        if len(history) > self.max_history_per_user:
            removed_count = len(history) - self.max_history_per_user
            evicted = history[:removed_count]
            self._run_compactor(key, evicted)
            self._histories[key] = history[-self.max_history_per_user :]
            self.logger.debug(
                f"Trimmed {removed_count} old message(s) from slice {key}"
            )

    def _trim_by_tokens(self, key: Tuple[int, str], token_budget: int) -> int:
        """Pop oldest messages until char-proxy total fits ``token_budget``.

        Preserves message boundaries (never splits a message). Returns
        the number of messages removed. Hands evicted messages to the
        compactor as a single batch before dropping them — see
        :meth:`_run_compactor`.
        """
        history = self._histories[key]
        char_budget = max(0, token_budget) * _CHARS_PER_TOKEN
        # Always keep at least one message — better to slightly exceed
        # the budget than ship an empty history slice that defeats the
        # whole point of the sub-agent's memory.
        evict_count = 0
        running = sum(len(str(m.content)) for m in history)
        while running > char_budget and (len(history) - evict_count) > 1:
            running -= len(str(history[evict_count].content))
            evict_count += 1
        if evict_count == 0:
            return 0
        evicted = history[:evict_count]
        self._run_compactor(key, evicted)
        del history[:evict_count]
        return evict_count


def create_history_manager(
    max_history_per_user: int = 20,
    compactor: Optional[Compactor] = None,
    event_sink: Optional[Callable[[ThinkingEvent], None]] = None,
) -> ChatHistoryManager:
    """Create and return a chat history manager instance."""
    return ChatHistoryManager(
        max_history_per_user, compactor=compactor, event_sink=event_sink
    )

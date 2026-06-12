"""Agent management for the Scufris Bot — OpenCode-backed.

:class:`AgentManager` drives chat turns by:

1. Resolving (or creating) a per-``user_id`` OpenCode session.
2. Building a per-turn system prompt from the user's facts + summary
   (so ``remember`` / ``forget`` mutations land in the next turn
   without needing to recreate the session).
3. Streaming events from OpenCode's ``GET /event`` bus, accumulating
   the assistant's text reply from ``message.part.delta`` chunks
   while mapping side-channel events (tool calls, tool results,
   permissions) to :class:`ThinkingEvent` instances dispatched to
   the per-request listeners.

History storage is split:

- **OpenCode** owns the raw conversation per session (so it does not
  need to be re-sent each turn).
- **ChatHistoryManager** owns the Scufris-side window used by the
  compactor for fact extraction and the ``/stats`` view.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from .callbacks import ThinkingEvent
from .history import SCUFRIS_AGENT, ChatHistoryManager
from .memory_compactor import format_age
from .opencode_client import (
    OpenCodeClient,
    OpenCodeError,
    OpenCodeSessionError,
    OpenCodeStaleSessionError,
)
from .opencode_events import EventMapperState, extract_text_delta, map_opencode_event
from .session_store import SessionStore

ThinkingCallback = Callable[[ThinkingEvent], None]

DEFAULT_SYSTEM_PROMPT_BASE = (
    "You are Scufris, a personal assistant for the user. Reply concisely and helpfully."
)


class AgentManager:
    """Drives chat turns through an OpenCode daemon.

    One persistent session per ``user_id`` (cached in :attr:`_sessions`),
    created lazily on first message and deleted via
    :meth:`delete_session` from ``/v1/clear``. When a
    :class:`SessionStore` is supplied the map is also persisted to
    disk so server restarts no longer lose continuity (see
    ``tasks/20260610-105007``).
    """

    def __init__(
        self,
        client: OpenCodeClient,
        *,
        history_manager: Optional[ChatHistoryManager] = None,
        system_prompt_base: str = DEFAULT_SYSTEM_PROMPT_BASE,
        provider_id: Optional[str] = None,
        model_id: Optional[str] = None,
        per_request_tools: Optional[Dict[str, bool]] = None,
        session_store: Optional[SessionStore] = None,
    ) -> None:
        """
        Args:
            client: Async OpenCode HTTP client. Lifecycle owned by the
                caller (bootstrap constructs; FastAPI lifespan closes).
            history_manager: Shared history manager. When set,
                :meth:`process_message` records per-(user, scufris)
                invocations and per-tool call counts so ``/stats``
                shows traffic.
            system_prompt_base: First line of the per-turn system
                prompt. Facts + summary are appended (when present).
            provider_id, model_id: Forwarded to the client when not
                ``None``; otherwise the client's own defaults are used.
            per_request_tools: Tool allow/deny map merged into every
                ``chat_stream`` call. Defaults to whatever the client
                was configured with at construction time. Used to
                disable ``task`` / ``todoread`` / ``todowrite`` so
                OpenCode doesn't try to spawn its own sub-agents.
            session_store: Optional persistence backend for the
                ``user_id -> session_id`` map. ``None`` keeps the
                map in-memory only (legacy behaviour, used by tests).
                Production passes a :class:`SessionStore` so the
                map survives restarts.
        """
        self._client = client
        self._history_manager = history_manager
        self._system_prompt_base = system_prompt_base
        self._provider_id = provider_id
        self._model_id = model_id
        self._per_request_tools = dict(per_request_tools) if per_request_tools else None
        self._store = session_store
        # Seed the in-memory map from disk when a store is wired up.
        self._sessions: Dict[int, str] = (
            session_store.as_dict() if session_store is not None else {}
        )
        self._sessions_lock = asyncio.Lock()
        self._logger = logging.getLogger("scufris-bot.agent")
        if session_store is not None and self._sessions:
            self._logger.info(
                "AgentManager: restored %d session(s) from %s",
                len(self._sessions),
                session_store.path,
            )

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    @property
    def client(self) -> OpenCodeClient:
        return self._client

    @property
    def sessions(self) -> Dict[int, str]:
        """Read-only snapshot of the ``user_id → session_id`` map."""
        return dict(self._sessions)

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def get_or_create_session(self, user_id: int) -> str:
        """Return the OpenCode session id for ``user_id``; create on miss."""
        async with self._sessions_lock:
            sid = self._sessions.get(user_id)
            if sid is not None:
                return sid
            session = await self._client.create_session()
            sid_value = session.get("id")
            if not isinstance(sid_value, str) or not sid_value:
                raise OpenCodeSessionError(
                    "create_session: response missing string 'id' field"
                )
            self._sessions[user_id] = sid_value
            if self._store is not None:
                self._store.set(user_id, sid_value)
            self._logger.info(
                "user %s: created OpenCode session %s", user_id, sid_value
            )
            return sid_value

    async def delete_session(self, user_id: int) -> Optional[str]:
        """Delete the cached session for ``user_id`` (if any).

        Returns the deleted session id (``None`` when there was no
        cached session). Errors talking to OpenCode are logged but
        don't propagate — the in-memory and on-disk entries are
        dropped unconditionally so the next message creates a fresh
        session.
        """
        async with self._sessions_lock:
            sid = self._sessions.pop(user_id, None)
            if sid is not None and self._store is not None:
                self._store.pop(user_id)
        if sid is None:
            return None
        try:
            await self._client.delete_session(sid)
        except OpenCodeError:
            self._logger.warning(
                "delete_session(%s) failed; in-memory entry dropped anyway",
                sid,
                exc_info=True,
            )
        return sid

    async def prune_invalid_sessions(self) -> int:
        """Drop persisted entries whose session no longer exists upstream.

        Called once at startup (from the FastAPI lifespan) so a stale
        session id surviving a long downtime — e.g. OpenCode's data
        directory was wiped while we were down — doesn't cause every
        first turn to fail with ``OpenCodeStaleSessionError``.

        Returns the number of entries pruned. ``list_sessions()``
        failures (OpenCode unreachable) log a warning and return ``0``
        without mutating the map.
        """
        async with self._sessions_lock:
            if not self._sessions:
                return 0
            snapshot = dict(self._sessions)
        try:
            upstream = await self._client.list_sessions()
        except Exception as exc:  # noqa: BLE001 — best-effort startup hook
            self._logger.warning(
                "prune_invalid_sessions: list_sessions() failed (%s); "
                "leaving map intact",
                exc,
            )
            return 0
        upstream_ids = {
            sess.get("id") for sess in upstream if isinstance(sess.get("id"), str)
        }
        stale = {uid: sid for uid, sid in snapshot.items() if sid not in upstream_ids}
        if not stale:
            return 0
        async with self._sessions_lock:
            for uid, sid in stale.items():
                # Only drop if the entry still points at the same stale
                # id — a concurrent get_or_create_session may have
                # already replaced it.
                if self._sessions.get(uid) == sid:
                    self._sessions.pop(uid, None)
            if self._store is not None:
                self._store.replace_all(self._sessions)
        self._logger.info(
            "prune_invalid_sessions: dropped %d stale entries", len(stale)
        )
        return len(stale)

    # ------------------------------------------------------------------
    # Chat turn
    # ------------------------------------------------------------------

    async def process_message(
        self,
        messages: List[Dict[str, Any]],
        user_id: int,
        extra_callbacks: Optional[List[Any]] = None,
    ) -> str:
        """Process a chat turn and return the assistant's reply text.

        Args:
            messages: Full message list as produced by
                :meth:`ChatHistoryManager.get_history_with_new_message`.
                Only the last user message is forwarded to OpenCode;
                prior turns live in OpenCode's session and the
                facts/summary system messages are rebuilt into the
                ``system`` parameter independently.
            user_id: Caller's user id.
            extra_callbacks: Heterogeneous list of callback objects.
                Items that are themselves callable, or that expose a
                callable ``on_thinking`` attribute, become per-request
                :class:`ThinkingEvent` listeners. All other entries
                (e.g. legacy LangChain counter handlers) are
                silently ignored — they were tracking tool counts
                via callbacks; that bookkeeping now lives inside
                ``_stream_turn``.
        """
        if self._history_manager is not None:
            self._history_manager.record_invocation(user_id, SCUFRIS_AGENT)

        new_message = _extract_new_user_message(messages)
        if not new_message:
            raise ValueError("process_message: no user message found in messages list")

        listeners = _collect_listeners(extra_callbacks)
        system_prompt = self._build_system_prompt(user_id)

        try:
            return await self._stream_turn(
                user_id, new_message, system_prompt, listeners
            )
        except OpenCodeStaleSessionError:
            self._logger.info(
                "user %s: cached session stale; recreating and retrying once",
                user_id,
            )
            async with self._sessions_lock:
                self._sessions.pop(user_id, None)
                if self._store is not None:
                    self._store.pop(user_id)
            return await self._stream_turn(
                user_id, new_message, system_prompt, listeners
            )

    async def _stream_turn(
        self,
        user_id: int,
        message: str,
        system_prompt: str,
        listeners: List[ThinkingCallback],
    ) -> str:
        sid = await self.get_or_create_session(user_id)
        state = EventMapperState()
        text_chunks: List[str] = []

        async for raw in self._client.chat_stream(
            sid,
            message,
            system=system_prompt,
            provider_id=self._provider_id,
            model_id=self._model_id,
            tools=self._per_request_tools,
        ):
            delta = extract_text_delta(raw)
            if delta is not None:
                text_chunks.append(delta)
            tev = map_opencode_event(raw, state)
            if tev is None:
                continue
            if (
                tev.kind == "tool_result"
                and self._history_manager is not None
                and tev.text
            ):
                # tool_result.text is "<tool>" on success, or
                # "<tool> failed: …" on error.
                name = tev.text.split(" failed:", 1)[0].strip()
                if name:
                    try:
                        self._history_manager.record_tool_invocation(user_id, name)
                    except Exception:  # noqa: BLE001 — never break the turn
                        self._logger.exception("record_tool_invocation failed")
            _dispatch(listeners, tev, self._logger)

        result = "".join(text_chunks)
        self._logger.info(
            "user %s: turn complete (length=%d, session=%s)",
            user_id,
            len(result),
            sid,
        )
        return result

    # ------------------------------------------------------------------
    # System prompt assembly
    # ------------------------------------------------------------------

    def _build_system_prompt(self, user_id: int) -> str:
        """Compose the per-turn system prompt: base + facts + summary.

        Rebuilt every turn (cheap; sidesteps the "facts changed
        between turns" edge case where a previous turn's
        ``remember`` / ``forget`` would otherwise need a session
        recreate to take effect).
        """
        parts: List[str] = [self._system_prompt_base]
        if self._history_manager is not None:
            facts = self._history_manager.get_facts_with_meta(user_id)
            if facts:
                lines = ["Known facts about the user (slot-keyed, durable):"]
                for k, entry in facts.items():
                    lines.append(
                        f"- {k}: {entry.value} ({entry.source}, "
                        f"{format_age(entry.timestamp)})"
                    )
                parts.append("\n".join(lines))
            summary = self._history_manager.get_summary(user_id)
            if summary:
                parts.append(f"Earlier conversation summary: {summary}")
        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_new_user_message(messages: List[Dict[str, Any]]) -> str:
    """Return the most recent user-role message content, or ``""``.

    Walks the list in reverse so the canonical
    ``[..., {role: 'user', content: ...}]`` tail is found in O(1).
    """
    for entry in reversed(messages or []):
        if entry.get("role") != "user":
            continue
        content = entry.get("content", "")
        if isinstance(content, str) and content:
            return content
    return ""


def _collect_listeners(
    callbacks: Optional[List[Any]],
) -> List[ThinkingCallback]:
    """Pull ``on_thinking`` callables from heterogeneous callback objects.

    ``chat.py`` and ``tests/test_server.py`` historically pass a
    list of :class:`ToolCallbackHandler` instances; this helper
    extracts the user-visible listener while ignoring any LangChain
    state still attached. Plain callables in the list are accepted
    too so tests can pass a bare function.
    """
    listeners: List[ThinkingCallback] = []
    for cb in callbacks or []:
        if callable(cb):
            listeners.append(cb)
            continue
        on_thinking = getattr(cb, "on_thinking", None)
        if callable(on_thinking):
            listeners.append(on_thinking)
    return listeners


def _dispatch(
    listeners: List[ThinkingCallback],
    event: ThinkingEvent,
    logger: logging.Logger,
) -> None:
    for fn in listeners:
        try:
            fn(event)
        except Exception:  # noqa: BLE001 — never break the agent
            logger.exception("on_thinking listener raised")


def create_agent_manager(
    client: OpenCodeClient,
    *,
    history_manager: Optional[ChatHistoryManager] = None,
    system_prompt_base: str = DEFAULT_SYSTEM_PROMPT_BASE,
    provider_id: Optional[str] = None,
    model_id: Optional[str] = None,
    per_request_tools: Optional[Dict[str, bool]] = None,
    session_store: Optional[SessionStore] = None,
) -> AgentManager:
    """Build an :class:`AgentManager` over an :class:`OpenCodeClient`."""
    return AgentManager(
        client,
        history_manager=history_manager,
        system_prompt_base=system_prompt_base,
        provider_id=provider_id,
        model_id=model_id,
        per_request_tools=per_request_tools,
        session_store=session_store,
    )

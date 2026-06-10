"""Minimal chat-message dataclass for Scufris's history layer.

Replaces ``langchain_core.messages.{Human,AI,System,Tool}Message`` for
the purpose of the OpenCode-runtime history layer. The OpenCode
runtime owns the real conversation; we only keep messages here for
the compactor's eviction window and the prompt-side context
(facts + summary).

Mirrors the OpenAI / Ollama wire shape (``{role, content}``) so the
same instance can be passed straight into the compactor's HTTP
transport without an extra mapping step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# The four roles we ever emit. ``"tool"`` is unused by the current
# OpenCode-driven runtime but kept in the type so future tool-result
# replays don't need a schema migration.
Role = Literal["user", "assistant", "system", "tool"]


@dataclass(frozen=True)
class HistoryMessage:
    """A single role/content pair in the chat history.

    Always carries a ``str`` ``content`` (LangChain's ``BaseMessage``
    allowed list-of-parts content; we deliberately drop that here —
    Scufris only stored plain strings anyway).
    """

    role: Role
    content: str


def system_message(content: str) -> HistoryMessage:
    """Build a system-role :class:`HistoryMessage`."""
    return HistoryMessage(role="system", content=content)


def user_message(content: str) -> HistoryMessage:
    """Build a user-role :class:`HistoryMessage`."""
    return HistoryMessage(role="user", content=content)


def assistant_message(content: str) -> HistoryMessage:
    """Build an assistant-role :class:`HistoryMessage`."""
    return HistoryMessage(role="assistant", content=content)

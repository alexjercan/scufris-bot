"""Per-user concurrency control + compaction event routing.

The agent runtime is a shared in-process resource so we serialise
requests *per user* (not globally): two different users can run turns
in parallel, but a single user's second turn waits for the first to
finish. This mirrors the CLI/Telegram semantics where a user has at
most one in-flight turn.

Compaction events emitted by the history manager carry no user id
(see ``ChatHistoryManager.set_event_sink``). To route them back to
the right SSE stream we stash the *current user* in a ContextVar at
turn start; the global event sink looks it up and dispatches to any
per-user listeners that have registered themselves.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextvars import ContextVar
from typing import Callable, Dict, List

from utils.callbacks import ThinkingEvent

# Per-user lock map. ``defaultdict(asyncio.Lock)`` is fine because new
# locks are cheap and we never need to evict — at most a few thousand
# user ids over the process lifetime.
_user_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def get_user_lock(user_id: int) -> asyncio.Lock:
    """Return the asyncio.Lock guarding ``user_id``'s turns."""
    return _user_locks[user_id]


# Current user id for the in-flight turn — used to attribute history
# manager events that don't carry a user id themselves.
current_user_id: ContextVar[int | None] = ContextVar("current_user_id", default=None)


# user_id -> list of listeners. Multiple SSE streams could (in
# principle) be open for the same user; we fan out to all of them.
_user_sinks: Dict[int, List[Callable[[ThinkingEvent], None]]] = defaultdict(list)


def add_user_sink(user_id: int, sink: Callable[[ThinkingEvent], None]) -> None:
    _user_sinks[user_id].append(sink)


def remove_user_sink(user_id: int, sink: Callable[[ThinkingEvent], None]) -> None:
    sinks = _user_sinks.get(user_id)
    if not sinks:
        return
    try:
        sinks.remove(sink)
    except ValueError:
        pass
    if not sinks:
        _user_sinks.pop(user_id, None)


def dispatch_event(event: ThinkingEvent) -> None:
    """Global history-manager event sink.

    Looks at the current-user ContextVar and forwards the event to any
    per-user listeners. Best-effort: never raises into the caller (the
    history manager runs this from inside a turn).
    """
    user_id = current_user_id.get()
    if user_id is None:
        return
    for sink in list(_user_sinks.get(user_id, ())):
        try:
            sink(event)
        except Exception:  # pragma: no cover — never break the agent
            pass

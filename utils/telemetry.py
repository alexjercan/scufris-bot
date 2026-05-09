"""Append-only JSONL telemetry for sub-agent invocations.

Off by default. Enable with the ``SCUFRIS_TELEMETRY=1`` environment
variable. When disabled, every entry point in this module is a near-
no-op (single env-var check).

Design notes
------------

The aim is to capture enough about each delegation to inform later
tuning of per-agent context budgets and trim policies — *without*
shipping a metrics pipeline. Output is one JSON object per line in
``logs/sub_agent_telemetry.jsonl``; analysis is ad-hoc with ``jq``
or pandas.

Per-event schema (see also ``tasks/20260509-165516``)::

    {
      "ts": "2026-05-09T17:55:16Z",
      "user_id": "telegram:12345" | "cli:local",
      "turn_id": "<uuid>",          # groups all delegations from one user msg
      "parent_agent": "scufris",     # future-proof for nested delegations
      "child_agent": "knowledge_agent",
      "query_chars": 47,
      "context_chars": 0,
      "context_present": false,
      "outcome": "ok" | "refused" | "error",
      "duration_ms": 1234
    }

Char counts are used as a token proxy to avoid a tokenizer dependency.

Per-turn correlation
--------------------

Both ``cli.py`` and ``main.py`` call :func:`begin_turn` once per
top-level user message. The returned context manager (or token,
when used directly) sets the ``turn_id`` and ``user_id`` contextvars
that the callback handler reads at sub-agent start/end. This avoids
threading IDs through the ``AgentManager.process_message`` /
LangChain ``invoke`` boundary.

Log rotation
------------

When the file exceeds 10 MB, it's renamed to ``.1`` (any existing
``.1`` is dropped). No deeper history kept — this is dev telemetry,
not an audit log.
"""

from __future__ import annotations

import contextvars
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_PATH = _LOG_DIR / "sub_agent_telemetry.jsonl"
_ROTATE_BYTES = 10 * 1024 * 1024  # 10 MB


def is_enabled() -> bool:
    """Return ``True`` iff ``SCUFRIS_TELEMETRY`` is set to a truthy value.

    Truthy values: ``1``, ``true``, ``yes``, ``on`` (case-insensitive).
    Anything else — including unset — disables telemetry.
    """
    val = os.environ.get("SCUFRIS_TELEMETRY", "").strip().lower()
    return val in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Per-turn context (read by the callback handler at sub-agent start/end)
# ---------------------------------------------------------------------------

_turn_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "scufris_turn_id", default=None
)
_user_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "scufris_user_id", default=None
)


def current_turn_id() -> Optional[str]:
    return _turn_id.get()


def current_user_id() -> Optional[str]:
    return _user_id.get()


@contextmanager
def begin_turn(user_id: str) -> Iterator[str]:
    """Bind a fresh ``turn_id`` (and the supplied ``user_id``) to the
    current async context for the duration of one top-level user message.

    Usage::

        with begin_turn(user_id):
            await agent_manager.process_message(...)

    Yields the generated ``turn_id`` so callers can log it themselves
    if useful. When telemetry is disabled the context vars are still
    set — cheap, and lets future code rely on ``current_turn_id()``
    being populated regardless of the telemetry flag.
    """
    turn_id = uuid.uuid4().hex
    tok_t = _turn_id.set(turn_id)
    tok_u = _user_id.set(user_id)
    try:
        yield turn_id
    finally:
        _turn_id.reset(tok_t)
        _user_id.reset(tok_u)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _rotate_if_needed() -> None:
    """Rename the log to ``.1`` if it exceeds ``_ROTATE_BYTES``.

    Best-effort: any OS error here is silently swallowed because this
    is dev telemetry and must never break a user-facing turn.
    """
    try:
        if not _LOG_PATH.exists():
            return
        if _LOG_PATH.stat().st_size < _ROTATE_BYTES:
            return
        rotated = _LOG_PATH.with_suffix(_LOG_PATH.suffix + ".1")
        if rotated.exists():
            rotated.unlink()
        _LOG_PATH.rename(rotated)
    except OSError:
        pass


def log_sub_agent_event(
    *,
    child_agent: str,
    query_chars: int,
    context_chars: int,
    outcome: str,
    duration_ms: int,
    parent_agent: str = "scufris",
) -> None:
    """Append a single sub-agent event to the JSONL log.

    No-op when :func:`is_enabled` returns ``False``. Failures while
    writing are swallowed — telemetry must never break a turn.
    """
    if not is_enabled():
        return
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed()
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "user_id": current_user_id(),
            "turn_id": current_turn_id(),
            "parent_agent": parent_agent,
            "child_agent": child_agent,
            "query_chars": query_chars,
            "context_chars": context_chars,
            "context_present": context_chars > 0,
            "outcome": outcome,
            "duration_ms": duration_ms,
        }
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Refusal detection
# ---------------------------------------------------------------------------


def is_refusal(output: str) -> bool:
    """Return ``True`` if a sub-agent output starts with the documented
    ``cannot_handle:`` refusal prefix (case-insensitive, leading
    whitespace tolerated).

    See ``SUB_AGENT_MEMORY_CONTEXT`` in ``utils/agent_builder.py``.
    """
    if not isinstance(output, str):
        return False
    return output.lstrip().lower().startswith("cannot_handle:")

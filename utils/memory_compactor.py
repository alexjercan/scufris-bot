"""Memory compaction protocol for the Scufris chat history.

When the history sliding window evicts messages, those messages are
handed to a :class:`Compactor` first so durable user information can
be salvaged into:

- a running per-(user, agent) **summary** of the older conversation,
- a per-(user, agent) **facts hashmap** of slot-keyed durable facts.

Phase 2 ships the :class:`LLMCompactor` (Ollama-backed, conservative
single-prompt template) plus a :func:`create_compactor` factory with
a ``SCUFRIS_COMPACTOR=noop`` env opt-out for A/B comparison.

Per-fact provenance: facts are stored as :class:`FactEntry` records
carrying ``value``, ``source`` (``"compactor"`` or ``"remember"``)
and a unix ``timestamp``. The ``Compactor`` interface itself stays
provenance-agnostic — it sees and returns plain ``Dict[str, str]``;
the history manager wraps incoming facts with ``source="compactor"``
on merge.

See ``tasks/20260509-162614/TASK.md`` (the spike) for the full
design. The LangChain stack was removed in
``tasks/20260610-105002``; the compactor now talks to Ollama via
``httpx`` against ``POST /api/chat`` directly.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Protocol, TypedDict

from .messages import HistoryMessage

_logger = logging.getLogger("scufris-bot.memory_compactor")

FactSource = Literal["compactor", "remember"]


@dataclass(frozen=True)
class FactEntry:
    """A single fact with provenance metadata.

    - ``value``: the fact's value (free-form short string).
    - ``source``: where it came from. ``"compactor"`` means the
      :class:`LLMCompactor` extracted it from evicted messages;
      ``"remember"`` means an agent called the ``remember`` tool
      (Phase 3) to write it explicitly.
    - ``timestamp``: unix epoch (seconds) at insertion time. Lets
      ``/stats`` and the prompt-side fact rendering show "2m ago".
    """

    value: str
    source: FactSource
    timestamp: float


class CompactionResult(TypedDict):
    """The output contract of a single :meth:`Compactor.compact` call.

    - ``summary``: the *complete* updated summary string (not a
      delta). The history manager replaces ``_summaries[key]`` with
      this value (subject to a hard char cap).
    - ``facts``: facts to *merge* into ``_facts[key]`` (last-write-
      wins on key collision; never a full replacement). The history
      manager wraps each entry with ``source="compactor"`` and the
      current timestamp.
    """

    summary: str
    facts: Dict[str, str]


class Compactor(Protocol):
    """Pluggable strategy for salvaging info from evicted messages."""

    def compact(
        self,
        evicted: List[HistoryMessage],
        existing_summary: str,
        existing_facts: Dict[str, str],
    ) -> CompactionResult:
        """Return updated summary + new facts given evicted messages.

        Implementations must be **total**: they may never raise.
        Eviction depends on the compactor returning, even if it has
        nothing useful to contribute. Errors should be caught
        internally and degrade to an empty result.

        ``existing_facts`` is a value-only view (provenance stripped)
        so implementations don't need to know about :class:`FactEntry`.
        """
        ...


class NoopCompactor:
    """Default compactor: preserves state, salvages nothing.

    Useful as the test-mode compactor when behaviour-equivalence
    with pre-compaction history is required, and as the
    ``SCUFRIS_COMPACTOR=noop`` opt-out target.

    The contract is "return what you want the new summary to be":
    returning ``existing_summary`` and an empty facts dict means
    "leave both layers untouched", which is exactly the no-op
    semantics we want.
    """

    def compact(
        self,
        evicted: List[HistoryMessage],
        existing_summary: str,
        existing_facts: Dict[str, str],
    ) -> CompactionResult:
        return {"summary": existing_summary, "facts": {}}


# ---------------------------------------------------------------------------
# LLM-backed compactor (Phase 2)
# ---------------------------------------------------------------------------

# Conservative prompt: tight JSON schema, instructs the model to be
# pessimistic about facts (omit when unsure) and to compress the
# summary (don't just append). Hard caps mirror history.py's
# `_SUMMARY_CHAR_CAP` so the LLM doesn't waste tokens producing
# output that will be clipped anyway.
_COMPACTION_PROMPT = """\
You compact an evicted slice of a chat history into:
1. A short running summary of *all* prior context (rewrite, don't append).
2. A hashmap of durable user facts (e.g. location, preferences, goals).

Update rules:
- Summary: <= 1500 chars. Compress aggressively. Keep what matters
  for future turns; drop chit-chat.
- Facts: only include things the user explicitly stated as durable
  (e.g. "I live in X", "I prefer Y", "my name is Z"). Skip ephemeral
  state (today's mood, current task). When unsure, OMIT.
- Keys: snake_case, <= 32 chars, in English. Reuse existing keys
  when overwriting.
- Output STRICT JSON. No prose, no markdown fences. Schema:
  {{"summary": "...", "facts": {{"key": "value", ...}}}}

Current summary (may be empty):
{existing_summary}

Current facts (may be empty):
{existing_facts_json}

Evicted messages (oldest first):
{evicted_text}

JSON output:
"""


def _format_evicted(evicted: List[HistoryMessage]) -> str:
    """Render evicted messages as a compact role/content transcript."""
    lines: List[str] = []
    for msg in evicted:
        content = msg.content.strip()
        if not content:
            continue
        lines.append(f"{msg.role}: {content}")
    return "\n".join(lines) if lines else "(no textual content)"


def _parse_compaction_json(raw: str, existing_summary: str) -> CompactionResult:
    """Parse the LLM's JSON output defensively.

    On any malformed output, returns a no-op result preserving
    ``existing_summary``. Logs at WARNING so quality issues are
    visible during dogfooding.
    """
    text = (raw or "").strip()
    # Strip accidental markdown fences. Models love them despite
    # being told not to.
    if text.startswith("```"):
        text = text.strip("`")
        # remove leading "json" tag if present
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        _logger.warning(
            "LLMCompactor: malformed JSON from model (len=%d), "
            "preserving existing summary",
            len(text),
        )
        return {"summary": existing_summary, "facts": {}}
    if not isinstance(data, dict):
        _logger.warning("LLMCompactor: JSON root is not an object; no-op")
        return {"summary": existing_summary, "facts": {}}
    summary = data.get("summary", existing_summary)
    facts = data.get("facts", {})
    if not isinstance(summary, str):
        summary = existing_summary
    if not isinstance(facts, dict):
        facts = {}
    # Coerce facts to {str: str}; drop anything else.
    clean_facts: Dict[str, str] = {}
    for k, v in facts.items():
        if isinstance(k, str) and isinstance(v, (str, int, float, bool)):
            clean_facts[k] = str(v)
    return {"summary": summary, "facts": clean_facts}


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class OllamaChatTransport:
    """Sync HTTP transport for Ollama's ``POST /api/chat`` endpoint.

    Replaces ``langchain_ollama.ChatOllama`` for the compactor's
    purposes. Exposes a single :meth:`chat` method that takes a
    ``messages: list[{role, content}]`` list and returns the
    assistant's textual response.

    The compactor only needs a single-shot non-streaming completion
    so we set ``stream=False`` and read the full body in one go.
    Errors propagate as ``httpx``-native exceptions and are caught
    by :class:`LLMCompactor` (the compactor is total — it must
    never raise).

    HTTP timeouts match LangChain's defaults (60s connect/read);
    the compactor is off the hot path, so this is generous.
    """

    DEFAULT_TIMEOUT_SECONDS = 60.0

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "http://localhost:11434",
        temperature: float = 0.0,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.timeout = timeout

    def chat(self, messages: List[Dict[str, str]]) -> str:
        """POST ``messages`` to ``/api/chat`` and return assistant text.

        ``messages`` must follow the Ollama / OpenAI wire shape
        (``[{"role": "...", "content": "..."}, ...]``).
        """
        # Lazy import so the module can be imported in tests / env
        # where httpx isn't strictly required (NoopCompactor path).
        import httpx

        payload = {
            "model": self.model,
            "messages": list(messages),
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        with httpx.Client(timeout=self.timeout) as http:
            response = http.post(f"{self.base_url}/api/chat", json=payload)
        response.raise_for_status()
        data = response.json()
        # Ollama /api/chat (non-streaming) shape:
        #   {"message": {"role": "assistant", "content": "..."}, ...}
        msg = data.get("message") or {}
        content = msg.get("content")
        return content if isinstance(content, str) else ""


class LLMCompactor:
    """HTTP-backed compactor using a small conservative prompt.

    The ``transport`` argument exposes
    ``chat(messages: list[{role, content}]) -> str`` so any source
    of completions (Ollama, a stub for tests, a future OpenCode
    summarize wrapper) plugs in cleanly. :class:`OllamaChatTransport`
    is the production default.

    Never raises: any transport error or malformed output degrades to
    a no-op result that preserves the existing summary, with a
    WARNING log line for visibility.
    """

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    def compact(
        self,
        evicted: List[HistoryMessage],
        existing_summary: str,
        existing_facts: Dict[str, str],
    ) -> CompactionResult:
        if not evicted:
            return {"summary": existing_summary, "facts": {}}
        prompt = _COMPACTION_PROMPT.format(
            existing_summary=existing_summary or "(none)",
            existing_facts_json=json.dumps(existing_facts, ensure_ascii=False),
            evicted_text=_format_evicted(evicted),
        )
        try:
            raw = self._transport.chat([{"role": "user", "content": prompt}])
        except Exception:
            _logger.warning(
                "LLMCompactor: transport raised; preserving existing summary",
                exc_info=True,
            )
            return {"summary": existing_summary, "facts": {}}
        if not isinstance(raw, str):
            raw = str(raw)
        return _parse_compaction_json(raw, existing_summary)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_compactor(
    model: Optional[str] = None,
    *,
    transport: Optional[Any] = None,
    base_url: Optional[str] = None,
) -> Compactor:
    """Create the default compactor, honouring ``SCUFRIS_COMPACTOR``.

    - ``SCUFRIS_COMPACTOR=noop`` → :class:`NoopCompactor` (A/B opt-out
      to fall back to Phase 1 behaviour without code changes).
    - Anything else (including unset) → :class:`LLMCompactor` wrapping
      an :class:`OllamaChatTransport`.

    Args:
        model: Ollama model name. Defaults to ``$SCUFRIS_COMPACTOR_MODEL``
            or ``"qwen2.5:3b"``. Only used when ``transport`` is not
            provided.
        transport: Pre-built transport instance (escape hatch for
            tests / when the caller wants to share an HTTP client
            across components). When given, ``model`` and ``base_url``
            are ignored.
        base_url: Ollama base URL. Defaults to ``$OLLAMA_BASE_URL`` or
            ``"http://localhost:11434"``. Only used when ``transport``
            is not provided.
    """
    mode = os.environ.get("SCUFRIS_COMPACTOR", "").strip().lower()
    if mode == "noop":
        _logger.info("create_compactor: SCUFRIS_COMPACTOR=noop → NoopCompactor")
        return NoopCompactor()
    if transport is not None:
        return LLMCompactor(transport)
    chosen_model = model or os.environ.get("SCUFRIS_COMPACTOR_MODEL") or "qwen2.5:3b"
    chosen_url = (
        base_url or os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434"
    )
    _logger.info(
        "create_compactor: LLMCompactor(model=%s, base_url=%s)",
        chosen_model,
        chosen_url,
    )
    return LLMCompactor(OllamaChatTransport(chosen_model, base_url=chosen_url))


# Helper for the history manager to construct FactEntry values with
# the current timestamp. Kept here (next to FactEntry) so the
# provenance contract lives in one place.
def make_fact_entry(value: str, source: FactSource) -> FactEntry:
    """Wrap a raw value with provenance + current timestamp."""
    return FactEntry(value=value, source=source, timestamp=time.time())


def format_age(timestamp: float, *, now: Optional[float] = None) -> str:
    """Render a unix timestamp as a short relative age (e.g. ``"2m ago"``).

    Used by the prompt-side facts renderer and ``/stats``. Pure /
    deterministic given ``now`` for testability.
    """
    current = now if now is not None else time.time()
    delta = max(0.0, current - timestamp)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"

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
design.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Protocol, TypedDict

from langchain_core.messages import BaseMessage

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
        evicted: List[BaseMessage],
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
        evicted: List[BaseMessage],
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


def _format_evicted(evicted: List[BaseMessage]) -> str:
    """Render evicted messages as a compact role/content transcript."""
    lines: List[str] = []
    for msg in evicted:
        role = getattr(msg, "type", msg.__class__.__name__)
        content = str(getattr(msg, "content", "")).strip()
        if not content:
            continue
        lines.append(f"{role}: {content}")
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


class LLMCompactor:
    """Ollama-backed compactor using a small conservative prompt.

    The ``llm`` argument must expose ``invoke(prompt: str) -> Any``
    (LangChain ``BaseChatModel`` / ``BaseLLM`` shape). Output text
    is read from ``.content`` if present, else ``str(response)``.

    Never raises: any LLM error or malformed output degrades to a
    no-op result that preserves the existing summary, with a WARNING
    log line for visibility.
    """

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    def compact(
        self,
        evicted: List[BaseMessage],
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
            response = self._llm.invoke(prompt)
        except Exception:
            _logger.warning(
                "LLMCompactor: LLM invocation raised; preserving existing summary",
                exc_info=True,
            )
            return {"summary": existing_summary, "facts": {}}
        raw = response.content if hasattr(response, "content") else str(response)
        if not isinstance(raw, str):
            raw = str(raw)
        return _parse_compaction_json(raw, existing_summary)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_compactor(
    model: Optional[str] = None,
    *,
    llm: Optional[Any] = None,
) -> Compactor:
    """Create the default compactor, honouring ``SCUFRIS_COMPACTOR``.

    - ``SCUFRIS_COMPACTOR=noop`` → :class:`NoopCompactor` (A/B opt-out
      to fall back to Phase 1 behaviour without code changes).
    - Anything else (including unset) → :class:`LLMCompactor`.

    Args:
        model: Ollama model name. Defaults to ``$SCUFRIS_COMPACTOR_MODEL``
            or ``"qwen2.5:3b"``. Only used when ``llm`` is not provided.
        llm: Pre-built LLM instance (escape hatch for tests / when the
            caller wants to share a model across components). When
            given, ``model`` is ignored.
    """
    mode = os.environ.get("SCUFRIS_COMPACTOR", "").strip().lower()
    if mode == "noop":
        _logger.info("create_compactor: SCUFRIS_COMPACTOR=noop → NoopCompactor")
        return NoopCompactor()
    if llm is not None:
        return LLMCompactor(llm)
    # Lazy import: keep test-only / noop paths free of the ollama
    # dependency, and avoid pulling it in at module import time.
    from langchain_ollama import ChatOllama  # type: ignore[import-untyped]

    chosen = model or os.environ.get("SCUFRIS_COMPACTOR_MODEL") or "qwen2.5:3b"
    _logger.info("create_compactor: LLMCompactor(model=%s)", chosen)
    return LLMCompactor(ChatOllama(model=chosen, temperature=0))


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

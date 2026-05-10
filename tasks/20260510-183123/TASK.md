# History compaction Phase 2: LLMCompactor + summary/facts injection into prompt assembly

- STATUS: CLOSED
- PRIORITY: 21
- TAGS: memory, compaction, phase2

> Phase 2 of 3 from the history-compaction spike
> (`tasks/20260509-162614`). Builds on Phase 1
> (`tasks/20260510-183121`) which added the `Compactor` Protocol and
> `NoopCompactor` scaffolding. This phase swaps in a real LLM-backed
> compactor and starts *using* the summary + facts in prompt
> assembly.

## Goal

After this phase ships:

- `LLMCompactor` runs a small Ollama call on every eviction, returns
  a `CompactionResult` (summary update + new facts).
- Bootstrap wires `LLMCompactor` (not `NoopCompactor`) by default.
- `get_history_with_new_message` and the sub-agent invocation path
  prepend a `SystemMessage` with known facts and another with the
  earlier-conversation summary, when non-empty.
- The agent can now actually *see* what was compacted.

## Backward compatibility

Adding context to a prompt should not regress quality — at worst
it's neutral, at best it preserves information the model would
otherwise lose to eviction. We will:

- Inject summary/facts only when non-empty (no empty SystemMessages
  bloating the prompt).
- Keep the order: `system_prompt → facts → summary → window →
  query`. This places durable user context closest to the system
  prompt where it has the most influence.
- Provide an env opt-out (`SCUFRIS_COMPACTOR=noop`) to fall back to
  Phase 1 behaviour for A/B if quality regresses on a particular
  agent.

## Scope

### In

- `utils/memory_compactor.py`:
  - Add `LLMCompactor(llm)` class implementing `Compactor`.
  - Conservative single-prompt template (see spike findings for the
    sketch). Returns strict JSON; parse defensively.
  - On JSON parse failure or LLM error: return
    `{"summary": existing_summary, "facts": {}}` (i.e. no-op for
    this round, log WARNING). Never raise.
  - Module-level `create_compactor(model: str | None = None) ->
    Compactor`. Reads `SCUFRIS_COMPACTOR` env: `"noop"` →
    `NoopCompactor()`; anything else (including unset) →
    `LLMCompactor(...)`.

- `utils/history.py`:
  - **Provenance refactor (folded in from user feedback):** change
    `_facts: Dict[Tuple[int,str], Dict[str, str]]` to
    `Dict[Tuple[int,str], Dict[str, FactEntry]]` where `FactEntry`
    is a frozen dataclass with `value: str`, `source: Literal["compactor", "remember"]`,
    `timestamp: float` (unix epoch). Lives in `utils/memory_compactor.py`
    next to `CompactionResult`.
  - `add_facts(user_id, agent, facts: Dict[str,str], source: str)`
    wraps each k/v with current timestamp + source on insert.
  - `get_facts` still returns a copy; callers can read `.value` /
    `.source` / `.timestamp` as needed.
  - `_run_compactor` passes `source="compactor"` when merging the
    compactor result.
  - Modify `get_history_with_new_message(user_id, new_message,
    agent)` to:
    1. Look up `summary = self.get_summary(user_id, agent)`.
    2. Look up `facts = self.get_facts(user_id, agent)`.
    3. Prepend `{"role": "system", "content": "Known facts: ..."}`
       if `facts` is non-empty. Render each as
       `key: value (source, Nm ago)` for traceability.
    4. Prepend `{"role": "system", "content": "Earlier
       conversation summary: ..."}` if `summary` is non-empty.
    5. Continue with existing window + query logic.

- Sub-agent invocation path (`utils/sub_agent.py` or wherever the
  per-agent message list is assembled before `agent.invoke(...)`):
  - Same prepending logic for `(user_id, sub_agent_name)` slice.

- Bootstrap (`main.py`, `cli.py`):
  - Replace `NoopCompactor()` with `create_compactor(...)`.
  - The compactor LLM defaults to the same small model
    `utilities_agent` uses (cheap, local).

### Out (Phase 3)

- `remember`/`forget` tools.
- `ThinkingEvent.compaction` variant.
- `/stats` columns.

## Acceptance criteria

- [x] `LLMCompactor` implemented with the conservative prompt.
- [x] Compactor never raises — all errors degrade to no-op + log.
- [x] `create_compactor` factory with `SCUFRIS_COMPACTOR=noop`
      opt-out.
- [x] `get_history_with_new_message` prepends facts and summary
      SystemMessages when non-empty, in the documented order.
- [x] Sub-agent invocation path does the same for its slice.
- [x] Bootstrap uses `create_compactor()` by default.
- [x] When summary/facts are empty (fresh user), prompt shape is
      identical to today (no empty SystemMessages).
- [x] All existing tests still pass.
- [x] New tests cover:
  - `LLMCompactor` happy path: mocked LLM returns valid JSON,
    `compact(...)` returns parsed result.
  - Malformed JSON from LLM → returns `{"summary": existing,
    "facts": {}}`, logs warning.
  - LLM raises → same fallback.
  - `create_compactor` honours `SCUFRIS_COMPACTOR=noop`.
  - `get_history_with_new_message` prepends facts when present,
    omits when empty.
  - Same for summary.
  - Order: facts come before summary when both present.
  - Sub-agent assembly path includes the same prepends.
  - Provenance: `FactEntry` populated with `source` + `timestamp`;
    compactor-sourced facts marked `"compactor"`; `add_facts`
    defaults to `"remember"`.
  - Markdown-fenced JSON output stripped.
  - Non-string scalars in facts coerced; non-string summary falls
    back to existing.
- [x] `ruff`, `pytest`, `mypy` all clean.

## Post-hoc notes

- **FactEntry refactor** folded in per user request for fact
  provenance ("where did we get that info from"). `_facts` is now
  `Dict[str, FactEntry]` internally; `get_facts` keeps its old
  `Dict[str, str]` value-only signature for BC, and a new
  `get_facts_with_meta` returns the full provenance view.
- **Prompt-side rendering** of facts shows
  `key: value (source, age)` per line. Phase 3's `/stats` and
  `ThinkingEvent.compaction` will consume the same metadata.
- **`LLMCompactor.invoke`** uses LangChain's standard `BaseChatModel`
  shape — no streaming, no callbacks. Defaults to `qwen2.5:3b` (cheap,
  local) but `SCUFRIS_COMPACTOR_MODEL` env can override.
- **Defensive parsing** strips markdown fences (models love them
  even when told not to), tolerates non-string scalars in facts
  (coerces to str), drops non-string fact values, and falls back
  to the existing summary on any non-string `summary` field.
- **Lazy import of `langchain_ollama`** in `create_compactor` so
  the noop / explicit-llm paths don't pull in the ollama dep at
  import time.
- **Compaction telemetry**: `_run_compactor` now logs an INFO line
  whenever it actually salvages something (non-empty summary or
  facts). Format: `[memory] <agent>: compacted N message(s),
  summary=Mch, +K fact(s)`. Phase 3 will upgrade this to a
  structured `ThinkingEvent.compaction` for CLI rendering.
- Final: 257 tests pass (229 baseline + 28 new), mypy clean across
  36 source files, ruff clean.

## Implementation notes

- LLM calls in tests must be mocked at the SDK boundary (per
  CLAUDE.md: no network, ≤5s total). Use the same pattern as
  existing sub-agent tests if any exist; otherwise mock at
  `langchain_ollama.ChatOllama` or whichever wrapper is in use.
- The compactor prompt is the hard part. Iterate manually with the
  real Ollama backend on dogfooding sessions; tune for:
  - JSON validity (high — failures are silent no-ops).
  - Conservative facts (omit when unsure).
  - Compressed summary (don't just append).
- Consider `pydantic` for the JSON schema if it pays off; otherwise
  `json.loads` + manual key validation is fine.

## References

- Spike: `tasks/20260509-162614/TASK.md` (Findings section).
- Phase 1 dependency: `tasks/20260510-183121/TASK.md`.

# History compaction Phase 1: Compactor protocol + summary/facts storage + eviction wiring

- STATUS: CLOSED
- PRIORITY: 22
- TAGS: memory, compaction, phase1

> Phase 1 of 3 from the history-compaction spike
> (`tasks/20260509-162614`). This is the **scaffolding** phase: it
> adds storage and the eviction → compactor wiring with a `Noop`
> compactor only. Behaviour-preserving by design — with `Noop`,
> `ChatHistoryManager` acts exactly as it does today.

## Goal

Add the structural pieces for the 3-layer memory model (window +
summary + facts) without touching prompt assembly or introducing any
LLM dependency. After this phase ships:

- `ChatHistoryManager` carries `_summaries` and `_facts` dicts.
- A new `utils/memory_compactor.py` defines the `Compactor` Protocol
  and a `NoopCompactor` default.
- Eviction in `_trim_by_tokens` and `_trim_history` invokes the
  compactor with the to-be-evicted messages before dropping them.
- Bootstrap (`main.py`, `cli.py`) wires `NoopCompactor` into
  `create_history_manager`. Behaviour observable to the user is
  unchanged.

Phase 2 swaps in `LLMCompactor` and starts injecting summary + facts
into prompt assembly. Phase 3 adds `remember`/`forget` tools.

## Backward compatibility

Per the user's instruction: this should strictly *not* regress
behaviour. The `NoopCompactor` returns empty results, so summary and
facts dicts stay empty, and prompt assembly is untouched in this
phase. Nothing the agent sees changes. Only internal data structures
grow.

## Scope

### In

- `utils/memory_compactor.py` (new):
  - `CompactionResult` TypedDict (`summary: str`, `facts: Dict[str, str]`).
  - `Compactor` Protocol with `compact(evicted, existing_summary, existing_facts) -> CompactionResult`.
  - `NoopCompactor` returning `{"summary": "", "facts": {}}`.

- `utils/history.py`:
  - Add `_summaries: Dict[Tuple[int, str], str]` (defaultdict(str)).
  - Add `_facts: Dict[Tuple[int, str], Dict[str, str]]` (defaultdict(dict)).
  - Constructor accepts optional `compactor: Compactor` (default
    `NoopCompactor()`).
  - Modify `_trim_by_tokens` and `_trim_history` to:
    1. Capture the messages about to be evicted.
    2. Call `compactor.compact(evicted, summary, facts)`.
    3. Merge the result into `_summaries[key]` and `_facts[key]`
       (last-write-wins on fact keys).
    4. Drop the messages from `_histories[key]`.
  - Wrap the compactor call in `try/except`: log a warning, proceed
    with eviction. The window remains source of truth.
  - Cap merged summary at 1500 chars (clip with ellipsis if exceeded).
  - Cap facts at 20 entries per slice (drop oldest by insertion
    order on overflow — use an `OrderedDict`-like discipline).
  - `clear_user(user_id)` also wipes `_summaries` and `_facts` for
    that user.
  - New accessors:
    - `get_summary(user_id, agent) -> str`
    - `get_facts(user_id, agent) -> Dict[str, str]`
    - `add_facts(user_id, agent, facts: Dict[str, str]) -> None`
    - `remove_fact(user_id, agent, key: str) -> bool`

- `main.py` and `cli.py` bootstrap: pass `NoopCompactor()` to
  `create_history_manager`.

### Out (Phase 2 / Phase 3)

- `LLMCompactor` and the prompt template.
- Injecting summary/facts into messages sent to the agent.
- `remember`/`forget` tools.
- `ThinkingEvent.compaction` variant.
- `/stats` columns for summary length / fact count.
- JSON persistence.
- Cross-slice fact propagation.

## Acceptance criteria

- [x] `utils/memory_compactor.py` exists with `CompactionResult`,
      `Compactor` Protocol, and `NoopCompactor`.
- [x] `ChatHistoryManager.__init__` accepts optional `compactor`,
      defaulting to `NoopCompactor()`.
- [x] `_trim_by_tokens` invokes the compactor with evicted messages
      before dropping them.
- [x] `_trim_history` does the same.
- [x] Compactor errors are caught, logged at WARNING, and do not
      block eviction.
- [x] Summary cap (1500 chars) and facts cap (20 entries) enforced
      after merge.
- [x] `clear_user` wipes `_summaries` and `_facts` for the user.
- [x] `get_summary`, `get_facts`, `add_facts`, `remove_fact`
      accessors implemented.
- [x] `main.py` and `cli.py` pass `NoopCompactor()` at bootstrap.
- [x] All 204 existing tests still pass (no behaviour regression).
- [x] New tests cover:
  - Storage round-trip for summary and facts.
  - Eviction triggers compactor with the right evicted messages.
  - Compactor result is merged into `_summaries`/`_facts`.
  - Facts cap drops oldest on overflow.
  - Summary cap clips on overflow.
  - Compactor exception is logged and eviction proceeds.
  - `clear_user` wipes new dicts.
  - `add_facts` last-write-wins; `remove_fact` returns False for
    missing key.
- [x] `ruff`, `pytest`, `mypy` all clean after the change.

## Post-hoc notes

- **NoopCompactor must echo `existing_summary`**, not return `""`.
  The compactor result *replaces* `_summaries[key]` on every
  eviction, so returning empty would wipe any user-supplied summary.
  Codified in tests.
- **Facts merge is last-write-wins** on key collision; FIFO
  eviction on overflow past `_FACTS_ENTRY_CAP`.
- **Summary clipping** uses ellipsis (`…`) suffix at
  `_SUMMARY_CHAR_CAP` (1500 chars).
- Final: 229 tests pass (204 baseline + 25 new), mypy clean across
  35 source files, ruff clean.

## Implementation notes

- The trim methods are delicate — there's already a
  `_trim_by_tokens` that pops one message at a time. Refactor so it
  collects all evictions in a single batch, hands them to the
  compactor once, then commits the deletion. Avoid calling the
  compactor per-message.
- `defaultdict` shenanigans: be careful that `_facts[key]` doesn't
  accidentally create empty entries from read-only access in tests.
  Prefer `.get(key, {})` for reads.
- For the facts cap, plain `dict` preserves insertion order in
  Python 3.7+, so an explicit `OrderedDict` isn't needed. Just
  `for k in list(facts)[:overflow]: del facts[k]`.
- Mock the compactor at the boundary in tests using a tiny
  `RecordingCompactor` test double rather than mocking
  `NoopCompactor` itself.

## References

- Spike: `tasks/20260509-162614/TASK.md` (Findings section).
- Current implementation: `utils/history.py`.

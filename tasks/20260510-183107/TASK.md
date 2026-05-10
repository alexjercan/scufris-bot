# History compaction Phase 3: remember/forget tools + thinking-trace + /stats polish

- STATUS: CLOSED
- PRIORITY: 20
- TAGS: memory, compaction, phase3

> Phase 3 of 3 from the history-compaction spike
> (`tasks/20260509-162614`). Builds on Phase 1
> (`tasks/20260510-183121`) and Phase 2 (`tasks/20260510-183123`).
> Adds the explicit-write side of the facts hashmap (`remember` /
> `forget` tools), surfaces compaction events in the CLI thinking
> trace, and extends `/stats` with the new memory columns.

## Goal

After this phase ships:

- Every agent (main + sub-agents) has access to `remember(key,
  value)` and `forget(key)` tools that write to *its own* slice of
  `_facts`.
- The CLI thinking trace emits a `compaction` event when the
  compactor runs, showing `(agent, n_evicted, n_new_facts)`.
- `/stats` adds two columns: `summary_chars` and `facts_count` per
  `(user, agent)` slice.
- Each agent's prompt is updated to authorise and explain the new
  tools.

## Backward compatibility

Adding tools is additive — the agent only calls them when prompted
to. Adding columns to `/stats` is purely informational. The
`compaction` event is opt-in to the consumer (the existing CLI
renderer can ignore unknown event variants).

## Scope

### In

- `utils/agent_builder.py` (or wherever per-agent toolsets are
  defined):
  - Inject `remember` and `forget` tools into every agent's
    toolset, including `scufris` (the main agent).
  - Tools resolve `(user_id, agent_name)` from `RunnableConfig`
    the same way per-agent history does today, then call
    `history_manager.add_facts(...)` / `remove_fact(...)`.
  - If an agent has `keeps_history=False` (e.g. `utilities_agent`),
    do *not* inject the tools. Facts only make sense for agents
    with persistent slices.

- New tools (location TBD — likely
  `utils/tools/memory_tools.py` since other tools live under
  `utils/tools/`):
  - `remember(key: str, value: str) -> str`. Returns
    confirmation string (e.g. `"remembered: location = Bucharest"`).
    Validates: `key` non-empty, ≤32 chars; `value` non-empty,
    ≤200 chars.
  - `forget(key: str) -> str`. Returns
    `"forgot: <key>"` or `"no such fact: <key>"`.

- Sub-agent / main-agent prompts:
  - Append a short paragraph authorising the tools and giving a
    one-line example. Do NOT add long memory-philosophy text — keep
    prompts tight.
  - Specifically state: "if the user retracts a fact, call
    `forget`; do not just stop using it".

- `utils/thinking.py` (or wherever `ThinkingEvent` lives):
  - Add a `compaction` variant carrying `(agent: str, evicted: int,
    new_facts: int)`.
  - Emitted from inside the compactor wiring in
    `ChatHistoryManager` (compactor itself stays decoupled — the
    history manager is responsible for telemetry).

- CLI renderer (`cli.py`):
  - Render `compaction` events as a single-line gray status:
    `[memory] knowledge_agent: compacted 3 messages, +1 fact`.

- `/stats` (`utils/stats.py`):
  - Add `summary_chars` and `facts_count` to the per-agent
    breakdown.
  - Pull from `history_manager.get_summary(...)` and
    `get_facts(...)`.

### Out (future / Phase 4)

- Deferred async compaction.
- Fact GC / dedup pass.
- JSON persistence.
- Cross-slice fact propagation (`forget` fan-out from main).

## Acceptance criteria

- [x] `remember` and `forget` tools implemented in
      `utils/tools/memory_tools.py`.
- [x] Tools validate input length and reject empty keys/values
      with a clear error string (returned as the tool result, not
      raised).
- [x] Tools resolve `user_id` and `agent_name` from
      `RunnableConfig`.
- [x] `agent_builder` injects them into every history-keeping agent.
- [x] Agents without history (`utilities_agent`) do NOT receive
      the tools.
- [x] Each affected agent's system prompt has a short paragraph
      authorising the tools.
- [x] `ThinkingEvent.compaction` variant added with
      `(agent, evicted, new_facts)` fields.
- [x] `ChatHistoryManager` emits the event when compaction runs
      (only when something actually changed — skip empty
      compactions).
- [x] CLI renders the event on a single line.
- [x] `/stats` shows `summary_chars` and `facts_count` columns.
- [x] All existing tests still pass.
- [x] New tests cover:
  - `remember` and `forget` happy path.
  - Validation errors return readable strings.
  - Tools route to the correct `(user_id, agent_name)` slice.
  - Tools NOT injected for `keeps_history=False` agents.
  - `compaction` event is emitted on non-empty compaction.
  - `/stats` includes the new columns.
- [x] `ruff`, `pytest`, `mypy` all clean.

## Implementation notes

- The tool needs a way to publish the `ThinkingEvent`. The history
  manager already has logger access; a new optional callback param
  on `ChatHistoryManager.__init__` (`event_sink: Callable[[ThinkingEvent], None]`)
  would mirror the pattern other components use. Defaults to a
  no-op.
- For `forget`: returning a friendly "no such fact" rather than
  raising lets the agent learn from the failure without halting.
- Prompt-update wording: keep it terse. One sentence per tool +
  one-line example. Avoid adding paragraphs — sub-agent prompts
  are already crowded.

## References

- Spike: `tasks/20260509-162614/TASK.md` (Findings section).
- Phase 1: `tasks/20260510-183121/TASK.md`.
- Phase 2: `tasks/20260510-183123/TASK.md`.

## Post-hoc notes

- **Tool factory closure pattern.** `make_memory_tools(history_manager,
  agent_name)` returns a fresh `[remember, forget]` pair per call;
  closures capture the slice key so callers don't have to thread it
  through. Tests inject a real `ChatHistoryManager`, which kept the
  test suite at the SDK boundary without needing additional fakes.
- **Event-sink wiring.** `ChatHistoryManager.set_event_sink()` setter
  was preferred over passing the sink to `__init__` because the
  bootstrap order in both `cli.py` and `main.py` creates the history
  manager before the callback handler / renderer. Using a setter kept
  the bootstrap diff minimal (one line in `cli.py`; main/Telegram is
  intentionally unwired since it has no thinking renderer today).
- **Telegram path left silent.** `main.py` does not install a sink —
  the Telegram bot doesn't render thinking events for users yet, so
  emitting `[memory] ...` to nowhere would be wasted work. Add the
  sink there if/when Telegram grows a thinking-trail feature.
- **`/stats` column expansion.** Added two columns (`summary` /
  `facts`) between `memory` and `calls` rather than at the end so
  the eye sees memory-related cells together. History-disabled
  agents render `—` (em-dash) in both columns to mean "not
  applicable" rather than a misleading `0ch / 0`.
- **Prompt updates kept terse.** Three sentences per affected agent
  (main + coding + knowledge + journal). Utilities deliberately
  skipped — pure-function agent, no history slice, no fact store.
- **No prompt update needed for the compactor source.** Phase 2
  already shipped `add_facts(..., source="remember")` as the public
  default — the new tool just calls it. The compactor wiring
  continues to pass `source="compactor"` explicitly.
- **Test count.** 257 → 276 (+19). All ruff / pytest / mypy clean.
- **CLI compaction render is depth-0.** Used a flat (non-indented)
  format because compaction events are not nested in any tool run —
  they come from the eviction path, which fires between turns.

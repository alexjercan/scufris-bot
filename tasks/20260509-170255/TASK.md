# Phase 3.5 — CLI thinking trace: render '+N prior turns' hint

- STATUS: OPEN
- PRIORITY: 60
- TAGS: phase3,cli,observability

> Foreshadowed in the master memory design doc, Decisions §5.
> Tiny observability touch on top of Phase 3.3 — surfaces in the CLI
> thinking trace how much prior history a sub-agent loaded for each
> call. Optional polish; ships independently.

## Scope

When a sub-agent loads N>0 prior messages from its history slice,
render a one-liner under the existing `tool_call` block, alongside
the Phase 2 `↳ context: ...` line:

```
→ knowledge_agent({"query": "weather forecast for Bucharest", "context": "..."})
  ↳ context: User previously asked about Ploiesti; comparing the two cities.
  ↳ +6 prior turns
```

When N=0 (cold start, or `keeps_history=False`), the line is omitted.

### Concrete changes

1. **`utils/callbacks.py` — `ThinkingEvent`.**
   Add an optional `prior_turns: Optional[int] = None` field
   alongside the existing `context` field.

2. **Where to populate it.** `on_tool_start` doesn't have visibility
   into the sub-agent's history slice — it runs *before*
   `sub_agent_tool` does. Two options:

   **A. Populate from inside `sub_agent_tool`** by emitting a custom
   event/log line. Requires reaching into the callback machinery from
   the tool — awkward.

   **B. Stash prior count on the tool's per-call state** (e.g. a
   weak dict keyed by `run_id` exposed by `BaseTool.run`'s contextvar
   patch) and read it on `on_tool_end`. Cleaner but only renders
   *after* the call completes, which is fine — the trace shows it on
   the same block.

   Recommend **B**: introduce a small `prior_turns_registry: Dict[UUID, int]`
   in `utils/callbacks.py` that `sub_agent_tool` writes to (passing
   `run_id` from the config), and `on_tool_end` pops to attach to the
   emitted event.

3. **`cli.py` — `render_thinking`.** When `ev.prior_turns` is a
   positive int, print `  ↳ +{prior_turns} prior turns` indented under
   the tool-call line. Skip when None or 0. Respect `--short-thinking`
   (no truncation needed — the line is already very short).

## Out of scope

- Telegram doesn't render the thinking trace; this is CLI-only (same
  as Phase 2's context rendering).
- Token-count display (Phase 4 if anyone asks).

## Acceptance criteria

- [ ] CLI thinking trace shows `↳ +N prior turns` line under
      sub-agent tool calls when N > 0.
- [ ] Cold-start (N = 0) calls omit the line entirely.
- [ ] `utilities_agent` (history off) never shows the line.
- [ ] No regression in Phase 2 `↳ context: ...` rendering.

## Estimated effort

~30 minutes. Mostly callback plumbing.

## Dependencies

Hard-blocks-on **3.3** (needs sub-agent history actually loading
something to count).

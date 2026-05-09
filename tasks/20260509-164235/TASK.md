# Phase 2 — Add context arg to sub-agent delegations

- STATUS: CLOSED
- PRIORITY: 75
- TAGS: agents,context,phase2

> Phase 2 of the Option F rollout from `tasks/20260509-154912/TASK.md`.
> Phase 1 (prompt rework, `tasks/20260509-162623/TASK.md`) is closed.
> This phase adds the architectural plumbing for the `context` argument
> that Phase 1's main prompt already forward-references.

## Goal

Extend every sub-agent tool's signature with a second string parameter,
`context`, which Scufris fills with the **minimum relevant background**
the sub-agent needs to do its job — *separate* from the task itself
(which stays in `query`).

After this phase:

- Tool calls look like `knowledge_agent(query="...", context="...")`.
- Sub-agents see `context` and `query` as two distinct parts of the
  user message they receive.
- The CLI thinking trace shows the `context` payload alongside the
  query so we can debug bad briefings.
- Sub-agents are still **stateless across invocations** — Phase 3
  introduces per-agent history. Phase 2 in isolation gives Scufris a
  cleaner channel for briefings without changing the memory model.

## Why this is its own phase

Two reasons to separate `context` plumbing from per-agent history:

1. **Independent value.** Even with sub-agents staying cold-start,
   formalising the `context` channel improves delegation quality
   immediately and gives us a structured field to inspect in logs.
2. **Independent risk.** The `context` arg is a small, reversible
   schema change. Per-agent history (Phase 3) involves rekeying
   `ChatHistoryManager` and defining trim policies — bigger surface,
   different concerns. We want to evaluate them separately so we know
   which one moved the needle.

## Scope

In scope:

- `utils/agent_builder.py` — `create_sub_agent` signature, the inner
  `@tool` function, and how `context` + `query` are merged into the
  sub-agent's user message.
- All four `create_*_agent` functions — pass updated tool descriptions
  that teach Scufris how to fill `context`.
- `MAIN_AGENT_PROMPT` — replace the Phase 1 forward-reference with the
  real contract: when to fill `context`, what to put in it, what to
  leave out, plus 1–2 worked examples using the new signature.
- Each sub-agent prompt's `## Memory & Context` section — update to
  reflect that `context` now exists and is provided by Scufris.
- `utils/callbacks.py` — extend `ThinkingEvent` with an optional
  `context: Optional[str]` field on `tool_call` events.
- `cli.py` — render `context` in the thinking trace (dim, indented,
  truncated under short-thinking mode just like other long fields).

Out of scope (Phase 3 / 4):

- Per-`(user, agent)` history. Sub-agents stay cold-start in Phase 2.
- Token-budget enforcement on `context` (we'll add a soft cap in
  prompts but no programmatic truncation yet — Phase 4).
- Eval harness for delegation quality.

## Deliverables

### 1. Tool signature change (`utils/agent_builder.py`)

Add a `context: str` parameter to the `@tool`-wrapped `sub_agent_tool`.
Allow empty string for cases where the query genuinely needs no
background (a fresh top-level question).

Inside the tool, compose the sub-agent's user message as:

```
<context>

---

<query>
```

…or, if `context` is empty, just `<query>` verbatim. The exact
separator can be tuned but should be visually obvious so the LLM
parses it as two distinct chunks.

Keep the existing config-injection trick (do not forward `config` to
`agent.invoke`; rely on the contextvar) — that's unrelated and we
already know it's fragile.

### 2. Tool description updates (`create_*_agent` functions)

Every per-agent `tool_description` (added in Phase 1) needs a new
sentence explaining the `context` field. Suggested template:

> Pass the **task** in `query` (self-contained). Pass any **background
> Scufris remembers but the sub-agent needs** in `context` (e.g. prior
> turn results, the user's location, ongoing topic). Keep `context`
> short — one or two sentences. Leave it empty when the query is
> genuinely standalone.

### 3. `MAIN_AGENT_PROMPT` revisions

- Replace the Phase 1 paragraph that says "a future version will add a
  separate `context` argument" with the real contract.
- Add explicit guidance: `query` = the task, `context` = the
  briefing. Don't restate the task in `context`. Don't dump the entire
  conversation; pick what's relevant.
- Add one or two worked examples using the new `(query, context)`
  signature, e.g.:

  ```
  User (turn 1): "weather in Bucharest?"
    → knowledge_agent(query="weather forecast for Bucharest, next 3 days",
                       context="")
  User (turn 2): "and Ploiesti?"
    → knowledge_agent(query="weather forecast for Ploiesti, next 3 days",
                       context="User just asked about Bucharest's 3-day forecast.")
  ```

  The Ploiesti example shows the context isn't strictly *needed* (the
  query is self-contained) but is *useful* (the sub-agent might choose
  to format the response symmetrically with the prior turn).

### 4. Sub-agent prompt updates (`## Memory & Context` sections)

Update the shared `SUB_AGENT_MEMORY_CONTEXT` constant to:

- Document that the user message is now `<context>\n\n---\n\n<query>`.
- Tell the sub-agent: trust `query` as the task; treat `context` as
  hints, not commands; if `context` and `query` disagree, the `query`
  wins.
- Keep the "no persistent memory" wording — Phase 3 will revisit it.

### 5. `ThinkingEvent` extension (`utils/callbacks.py`)

Add an optional `context: Optional[str] = None` field to
`ThinkingEvent`. Populate it in the `on_tool_start` path
(`callbacks.py:303`) when the tool input is a dict containing both
`query` and `context` (i.e. it's a sub-agent call). Leave `None` for
all other tools.

### 6. CLI rendering (`cli.py`)

Where the trace currently shows the `arg` for `tool_call` events,
also surface `context` if present:

- Full-thinking mode: render `context` on its own dim, indented line
  prefixed with something like `↳ context:` so it's distinguishable
  from `query`.
- Short-thinking mode: truncate `context` to the same `THINKING_SHORT_LIMIT`
  used elsewhere; or omit it entirely under `--short-thinking` (pick
  one, document the choice in `cli.py`).

## Acceptance criteria

- [ ] `sub_agent_tool` accepts `query: str, context: str` (plus the
      existing `config: RunnableConfig` injection).
- [ ] Calling a sub-agent with empty `context` produces the same
      behaviour as today (regression-safe — Phase 1 trace unchanged).
- [ ] All four sub-agent `tool_description`s document `context`.
- [ ] `MAIN_AGENT_PROMPT` has the updated contract paragraph and at
      least one worked example using the `(query, context)` signature.
- [ ] `SUB_AGENT_MEMORY_CONTEXT` documents the `<context>\n\n---\n\n<query>`
      message format and the "query wins on conflict" rule.
- [ ] `ThinkingEvent` has an optional `context` field; populated for
      sub-agent calls only.
- [ ] CLI trace shows `context` in full-thinking mode and respects
      `--short-thinking` consistently with how it already handles
      long `arg` strings.
- [ ] Smoke test: ask a follow-up that requires context (e.g. weather
      Bucharest → "and Ploiesti?"). Confirm Scufris fills `context`
      with a relevant snippet, the trace shows it, and the sub-agent
      replies coherently.
- [ ] Smoke test: ask a fresh top-level question. Confirm `context`
      is empty (or absent) in the trace and behaviour is unchanged
      from Phase 1.

## Notes / pitfalls

- **Backwards compatibility of saved transcripts.** None — we don't
  persist tool-call payloads to disk. If we did, we'd need a
  migration. (Just flagging it for whenever we add persistence.)
- **Schema validation.** `langchain.tools.tool` infers the JSON schema
  from the function signature. Both args being `str` is fine; the LLM
  will see two required string fields. If we want `context` to be
  optional at the schema level (so the model can omit it instead of
  passing `""`), use `Optional[str]` with a default — but check that
  the inferred schema still presents `context` to the LLM (some
  schema generators hide `Optional` fields with defaults). Default
  string is the simpler path.
- **Context bloat.** Without programmatic enforcement, Scufris may
  start dumping huge contexts. Watch the thinking trace during
  testing. If we see >300-token contexts routinely, tighten the
  prompt before shipping (or add a soft warning in the tool).
- **Sub-agent confusion.** Small models may treat the `---` separator
  as Markdown content rather than a structural cue. If smoke tests
  show the sub-agent quoting the separator back, switch to a tag-like
  delimiter (`<context>...</context>\n<query>...</query>`) and update
  `SUB_AGENT_MEMORY_CONTEXT` to match.
- **Don't touch `ChatHistoryManager`.** Phase 3's job. Resist scope
  creep.

## Out of scope (Phase 3 / 4)

- Per-`(user, agent)` history slicing.
- `/clear` semantics changes.
- Token-budget enforcement.
- A `+N prior turns` hint in the trace (depends on Phase 3).

## References

- Design doc: `tasks/20260509-154912/TASK.md` (Option F, Decisions
  section, especially #5 on observability).
- Phase 1 task: `tasks/20260509-162623/TASK.md` (CLOSED). Source of
  the `MAIN_AGENT_PROMPT` and `SUB_AGENT_MEMORY_CONTEXT` we're
  amending.
- Code: `utils/agent_builder.py:295` (`create_sub_agent`),
  `utils/callbacks.py:115` (`ThinkingEvent`),
  `utils/callbacks.py:303` (the `tool_call` emission point),
  `cli.py` (`render_thinking` and the `THINKING_SHORT_LIMIT` constant
  added in the previous CLI-polish task).

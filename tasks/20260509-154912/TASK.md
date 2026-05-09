# Multi-agent memory & prompt synergy: design discussion

- STATUS: OPEN
- PRIORITY: 70
- TAGS: design,agents,prompts,memory

> This is a **design doc / discussion**, not a plan-of-action yet.
> The goal is to align on a strategy for how Scufris and its sub-agents
> share (or don't share) context, and how prompts should evolve to
> support that. Concrete implementation tasks will be spun off from
> here once the strategy is agreed.

## Background — what we have today

### Architecture
- **Main agent**: `Scufris` (created in `setup_scufris`). It owns the
  user-facing conversation.
- **Sub-agents**: `coding_agent`, `knowledge_agent`, `utilities_agent`,
  `journal_agent`. Each is a `langchain.agents.create_agent` instance
  wrapped as a `@tool` by `create_sub_agent` in
  `utils/agent_builder.py:295`.

### How a sub-agent gets called
- The main LLM emits a tool call like
  `knowledge_agent({"query": "weather in Ploiesti"})`.
- `sub_agent_tool` runs
  `agent.invoke({"messages": [{"role":"user","content":query}]})`.
  → The sub-agent sees **exactly one user message**: the synthesized
  query string. Nothing else.
- The sub-agent's last message content is returned as the tool result.
  It becomes a `ToolMessage` in Scufris's transcript.
- The sub-agent process is then effectively GC'd; its internal
  `messages` list is discarded.

### Memory model
- `ChatHistoryManager` (`utils/history.py`) stores per-user history
  (a sliding window of `max_history_per_user` messages).
- **Only the main agent reads/writes this.** See `main.py:68` and
  `cli.py:121` — both call
  `history_manager.get_history_with_new_message(user_id, ...)` and
  feed the result to `agent_manager.invoke`.
- Sub-agents are stateless across invocations. Each call is a cold
  start: `[system_prompt, single_user_query]`.

### What the sub-agent actually "knows"
- Its system prompt (static, hard-coded per agent).
- The single `query` string the main LLM chose to send.
- Its own tool results from this invocation only.

### Concrete failure mode (the one motivating this task)
> Scufris hands off "do the same thing but for Ploiesti" to
> `knowledge_agent`. Knowledge has never seen the previous turn, so
> "the same thing" is meaningless. It either guesses, asks back via
> the tool result (awkward), or hallucinates.

This isn't a bug — it's the architecture. The sub-agent literally
cannot know what came before.

## What we want to improve

Phrased as goals, in rough priority order:

1. **Sub-agents should be useful, not zombies.** When invoked, they
   should have enough context to do the job in one shot in the common
   case.
2. **Don't leak unnecessary context.** Knowledge doesn't need to know
   the user logged 200g of chicken last Tuesday. Coding doesn't need
   the weather conversation. Context bleed = token waste + worse
   reasoning + privacy noise.
3. **Prompts should encode the contract.** Scufris's prompt should
   make clear *it* is the one with memory and *it* must phrase
   delegations as self-contained tasks. Sub-agent prompts should make
   clear they have no memory and should not ask back.
4. **Optional: per-session sub-agent memory.** Decide whether sub-
   agents should accumulate state across invocations within one user
   session (cheaper repeated lookups, smoother multi-turn drilldowns)
   or stay stateless workers (simpler, predictable, no leakage).

## Design space — pick a strategy

We don't have to pick one strategy globally. It's reasonable for
different sub-agents to have different memory policies. Below are the
options as discrete points on a spectrum, from least to most stateful.

### Option A — Stateless workers, smarter delegations (status quo, prompt-only fix)

- Sub-agents stay completely stateless.
- All the heavy lifting moves to **Scufris's prompt**: it must
  rephrase any delegation as a self-contained task. No "do the same
  thing", no anaphora ("that one", "it"), no implicit references.
- Sub-agent prompts gain an explicit "you have no memory" section
  telling them to refuse / ask via the response if the query is
  ambiguous — but in practice the goal is that the main agent never
  sends ambiguous queries.

**Pros**
- Zero architectural change. Just prompts.
- Cheapest in tokens — nothing extra crosses the boundary.
- Predictable. Sub-agent behaviour is a pure function of
  `(prompt, query, tools)`.
- Clean privacy story.

**Cons**
- Quality is bottlenecked on Scufris correctly reformulating queries.
  Small models will fail at this regularly.
- Forces every delegation to be a complete English sentence even when
  one would prefer a follow-up.

### Option B — Stateless, but with a structured "context" arg

- Add a second param to each sub-agent tool, e.g.
  `knowledge_agent(query, context)`. Scufris is taught (in its prompt
  + the tool's description) to fill `context` with the *minimum
  relevant background* — not the full transcript.
- Sub-agent receives `[system_prompt, context, user_query]` as three
  separate messages (or one composed message).

**Pros**
- Keeps sub-agents stateless (still cold-start each call).
- Makes the contract explicit and inspectable in logs ("here is what
  Scufris thought you needed to know").
- Easy to reason about token cost — context is bounded by what
  Scufris chose to pass.

**Cons**
- Requires Scufris to be good at summarising relevant context, which
  is the same skill as Option A's "good rephrasing". Marginal
  improvement unless the structured field genuinely helps the LLM
  think about it explicitly (it might).
- Need to update tool schemas + main agent prompt.

### Option C — Per-session sub-agent memory (per user, per agent)

- Extend `ChatHistoryManager` to be keyed by `(user_id, agent_name)`
  instead of just `user_id`.
- Each sub-agent invocation prepends its own per-(user, agent)
  history to the messages it sees.
- Main agent still owns `(user_id, "main")` — its own history.

**Pros**
- Sub-agents become coherent across multi-turn drilldowns
  ("knowledge, more on that", "knowledge, what about Bucharest
  instead").
- Cheap lookups: e.g. journal_agent already remembers it just
  created today's entry, no double `today_create_tool` call.

**Cons**
- Sub-agent memory is invisible to Scufris. Now there are *two*
  conversations the user is implicitly part of: one with Scufris,
  one with each sub-agent. Mental model gets weird.
- Failure mode: user says "delete that note" to Scufris; Scufris
  delegates to journal_agent; journal_agent has its own history that
  may disagree with Scufris's history about which "that" is current.
- Trim windows / clearing semantics to define: does `/clear` wipe
  sub-agent histories too? Probably yes, but per-user.
- Token cost grows: every sub-agent call now ships a transcript.

### Option D — Shared "session memory" object

- A single per-session memory object that all agents read from /
  write to. Could be:
  - A list of recent user messages + agent responses (similar to
    Scufris's history, but visible to sub-agents).
  - Or a structured scratchpad ("user is currently in Ploiesti",
    "user is logging dinner") that any agent can read or update.
- Sub-agent invocations pull from this when they need to.

**Pros**
- Genuinely shared world-model. No "Scufris said one thing, Knowledge
  thinks another".
- Lets us factor *facts* (location, current intent, recent topic) out
  of the chat transcript and into something queryable.

**Cons**
- Significantly more design work. Who writes to it, when, in what
  format? Conflict resolution? Staleness?
- Risks becoming a dumping ground that bloats every prompt.
- Without clear discipline, ends up looking like Option C with extra
  steps.

### Option E — Asymmetric: Scufris-history visible to sub-agents (read-only)

- Each sub-agent invocation receives **the recent N turns of the main
  conversation** as additional context, prepended (or summarised)
  into its messages.
- Sub-agents do not have their own history.
- Direction is one-way: sub-agent never writes to Scufris's history
  (its only output is the tool result, which Scufris incorporates
  into *its* next assistant turn).

**Pros**
- Solves the "do the same thing" problem at the architecture level
  rather than relying on Scufris to summarise correctly.
- Sub-agents stay simple (still no per-agent state to manage).
- One-way direction keeps the mental model: Scufris is the
  conversation, sub-agents are workers that occasionally need to peek
  at the conversation.

**Cons**
- Token cost on every sub-agent call (history shipped every time).
- Sub-agents see *everything*, including potentially sensitive turns
  irrelevant to them. Privacy/leakage concern (e.g. coding_agent
  seeing journal entries).
- Need a knob: how many turns? Just user messages, or assistant
  responses too? Summarise vs. raw?

### Option F — Per-agent history + Scufris-summarised context (B + C combined) ⭐ chosen direction

The synthesis of B and C. Each sub-agent keeps its own persistent
per-(user, agent) history (Option C), AND every delegation carries a
Scufris-authored `context` string (Option B). The sub-agent thus sees:

```
[ system_prompt,
  ...its own prior (user, agent) history...,
  { role: "user",
    content: "<context Scufris wrote for this turn>\n\n<query>" } ]
```

Crucial design clarification: **Scufris does NOT see the sub-agent's
internal history.** It writes `context` from its *own* memory — which
already contains the `ToolMessage` results of prior delegations to
this sub-agent. So:

- Scufris contributes the *curated, cross-cutting* context: "the user
  asked about Bucharest weather two turns ago; now they want Ploiesti
  with the same forecast horizon".
- The sub-agent contributes its *internal continuity*: its own
  reasoning steps, intermediate tool calls, and exact prior
  responses, none of which Scufris ever saw verbatim.

The two views are complementary, not redundant.

**Pros**
- Best of both worlds: sub-agent has full internal continuity (no
  redundant tool calls, remembers its own state) AND gets a
  task-shaped briefing from Scufris.
- Privacy cleanly preserved: each sub-agent only ever sees its *own*
  history. No cross-agent leakage. Scufris's summary acts as the
  controlled channel between domains.
- Failure-tolerant: even if Scufris writes a poor `context`, the sub-
  agent can still draw on its own past turns. Even if the per-agent
  history is empty (first-time call), `context` carries the briefing.
- `context` is inspectable in logs — easy to debug bad delegations.
- The mental model is symmetrical and explainable: Scufris owns the
  *user-facing* conversation; each sub-agent owns its *own*
  conversation; the `context` arg is the formal handoff between them.

**Cons**
- Highest implementation cost so far. Touches:
  - `ChatHistoryManager` (rekey by `(user_id, agent_name)`).
  - `create_sub_agent` (accept `context`, inject history).
  - All sub-agent tool descriptions (teach Scufris how to fill
    `context`).
  - Scufris's main system prompt (mandate `context` quality, explain
    that the sub-agent has its own memory).
  - CLI thinking trace (render the `context` payload).
  - `/clear` semantics (clear *all* per-agent histories for that
    user).
- Token cost per call grows: own-history + context. Need to bound
  both. (Probably: smaller window for sub-agent history than main;
  hard cap on `context` length.)
- Sub-agent histories may drift "out of sync" with reality — e.g.
  user changed topic in main but `journal_agent`'s history still
  reflects yesterday's logging session. Scufris's `context` is the
  intended cure but it's a soft one.
- The "what gets stored in sub-agent history" question is non-
  trivial: just user-turn + final response (cheap, lossy on
  reasoning), or full inner transcript including intermediate tool
  calls (expensive, full fidelity). Probably full transcript trimmed
  by token budget rather than message count.
- Risk that Scufris's `context` simply repeats things in the sub-
  agent's history, wasting tokens. The prompts must explicitly say
  *don't restate what the sub-agent already remembers; assume it has
  its own log of past calls*.

**Reading-#1 alternative (rejected, recorded for completeness):**
A naive interpretation of "Scufris summarises the sub-agent's
history" would inject each sub-agent's transcript into Scufris's
prompt so Scufris can read and summarise it. We're rejecting this
because (a) it bloats Scufris's context with information it already
has in tool-result form, (b) it breaks the privacy boundary the rest
of the design is built on, and (c) it makes Scufris's prompt grow
linearly with the number of sub-agents. Reading #2 (Scufris
summarises from its own memory of past tool results) is the chosen
interpretation.

## Chosen direction — Option F (B + C combined)

Phased rollout, smallest reversible step first:

1. **Phase 1 — Prompt prerequisites.** Even with Option F, the
   prompt work from Option A is still required and is a
   self-contained win we can ship first:
   - Rewrite Scufris's main prompt to make the delegation contract
     explicit ("you have memory; sub-agents have their *own* memory
     plus a `context` arg you write; never assume they share yours").
   - Add a `## Memory & Context` section to each sub-agent prompt
     declaring "you have a private history with this user; in
     addition Scufris will brief you via the `context` field; do not
     assume access to the main conversation".
   - Worked examples in Scufris's prompt of well-formed delegations.

   Phase 1 ships value even without the architectural changes and
   gives us a clean baseline to A/B against.

2. **Phase 2 — Add the `context` argument (B half of F).**
   Extend the `@tool` signature in `create_sub_agent` to accept a
   second string parameter `context`. Render it in the CLI thinking
   trace alongside the query. Sub-agent histories not yet wired up,
   so behaviour is still cold-start, but Scufris is already
   practising filling `context`. Lets us evaluate the quality of
   Scufris's briefings independently of the memory machinery.

3. **Phase 3 — Per-(user, agent) history (C half of F).**
   Rekey `ChatHistoryManager` (or introduce a parallel manager)
   by `(user_id, agent_name)`. Wire `create_sub_agent` to load and
   persist its own slice. `/clear` wipes everything for that user.
   Define and document the trim policy (likely token-budgeted, not
   message-count, given the variable size of tool results).

4. **Phase 4 — Tuning.** Per-agent windows, per-agent decisions on
   whether to store the full inner transcript vs. just user-turn +
   final response, and observability polish in the thinking trace.

Rollback path: Phase 2 and Phase 3 are independent. If Phase 3 turns
out to hurt more than it helps for a particular sub-agent, we can
disable per-agent history just for that one and keep `context`-only
behaviour (which is still strictly better than today).

## Acceptance criteria for *this design doc* (not the implementation)

- [x] We agree on Option F (B + C combined) as the strategic
      direction.
- [x] We agree on a phased rollout (1 prompts → 2 `context` arg →
      3 per-agent history → 4 tuning).
- [x] For each sub-agent we record an explicit memory policy (see
      table below).
- [x] Open questions either decided or explicitly deferred (see
      Decisions section).

### Per-agent memory policy

| Sub-agent          | Per-agent history | `context` arg | Notes |
|--------------------|-------------------|---------------|-------|
| `coding_agent`     | yes               | yes           | History helps avoid re-stating project layout; OpenCode does the heavy lifting. |
| `knowledge_agent`  | yes               | yes           | Highest expected uplift from continuity ("more on that", "what about X"). |
| `utilities_agent`  | no                | yes           | Calls are pure functions; history is just noise. Reconsider if we add stateful utilities. |
| `journal_agent`    | yes (largest)     | yes           | Strong case — multi-step daily flows. Probably wants the largest window. |

Implementation note: hard-code the policy per agent in
`create_sub_agent` for now (a simple flag like `keeps_history: bool`).
Promote to full configurability only if we need it.

## Delegation-failure protocol

Captured here because it cuts across all three options and shapes the
Phase 1 prompts:

- **Sub-agents may refuse.** If a sub-agent decides a request is
  outside its competence (e.g. user asks `journal_agent` to do a web
  search), it should return a short, structured refusal in its tool
  result rather than guessing or doing the wrong thing. Suggested
  shape: a one-line tag like `cannot_handle: <reason>` followed by
  whatever brief context helps Scufris re-route.
- **Scufris's fallback ladder.** On receiving a `cannot_handle`
  result, Scufris should:
  1. Try a more appropriate sub-agent if one exists.
  2. If none fits, attempt the task itself if it's something it can
     do without a tool.
  3. If still stuck, tell the user honestly: "I can't do that right
     now."
- This protocol must be documented in:
  - Each sub-agent's prompt (when and how to refuse).
  - Scufris's main prompt (how to interpret refusals and re-route).

This is a Phase 1 concern (prompt-only) and ships with the rest of
the prompt rework.

## Decisions

Closed items, captured for future-self reference. The original
discussion lives in the file history.

1. **What gets stored in sub-agent history.** Full inner transcript
   (intermediate tool calls included), trimmed by token budget.
2. **Trim policy.** Token-budget based, not message-count. Exact
   budget TBD (see Deferred). The current sliding-window approach is
   considered a starting point only — the strategy is expected to
   evolve (see *History compaction spike* in spin-offs).
3. **`/clear` semantics.** Wipes the user's main history AND every
   per-agent history for that user. No per-agent clear sub-command —
   keep it simple, this is a debugging tool.
4. **Privacy / cross-leakage.** Not a concern at current scale
   (single user). The Option F architecture preserves the no-leakage
   property by construction anyway, but we won't hand-craft prompt
   rules to enforce it. Revisit if usage scales or we observe quality
   regressions from leakage.
5. **Observability.** Extend the existing `ThinkingEvent` `tool_call`
   variant with an optional `context` field. Simpler to plumb, fewer
   moving parts than introducing a new event kind. Optionally add a
   small `+N prior turns` hint as a separate field once Phase 3 lands.
6. **Prompt slim-down on `journal_agent`.** Yes, run a parallel
   refactor pass during Phase 1.
7. **Testing.** Nice to have, not a blocker. Phase 4 task.
8. **Tool description vs. main prompt.** All sub-agent specifics
   (including how to fill `context`) live in the per-agent tool
   description. Scufris's main prompt stays high-level and intuitive
   ("pick the right agent based on the request; trust their refusals
   when they happen").

## Deferred (genuinely TBD)

These don't block any phase — pick sane defaults to start, tune from
real usage:

- **Token budget per sub-agent invocation.** Cap on
  (own-history + `context` + query). Will depend on the model
  (currently small Qwen via Ollama). Start with sane defaults
  (suggest: ~4k tokens per-agent history, ~500 token `context` cap),
  tune later.
- **Per-agent history window size.** Same as above. Start with
  uniform defaults, special-case `journal_agent` if it bumps the cap.


## Non-goals (for now)

- Cross-user memory or any kind of long-term persistence beyond
  the existing in-process `ChatHistoryManager`.
- Replacing `ChatHistoryManager` with a vector store / RAG / external
  memory backend.
- Letting sub-agents call each other directly (today only Scufris
  delegates).

## Spin-off tasks (to be created once we agree)

Mapped to the phased rollout above.

### Phase 1 — Prompts (no code/architecture changes)
- "Rewrite Scufris main system prompt: explain the F memory model
  (Scufris owns user history; each sub-agent has its own private
  history; cross-agent communication is via the `context` arg only)."
- "Add `## Memory & Context` section to each sub-agent prompt
  declaring its private history + Scufris-authored `context`."
- "Add 3–5 worked delegation examples (good vs bad) to Scufris's
  prompt."
- "Refactor sub-agent prompts: extract shared boilerplate, slim down
  `journal_agent`." (Parallelisable.)

### Phase 2 — Add `context` arg
- "Extend `create_sub_agent` to accept and inject a `context: str`
  arg into each sub-agent invocation."
- "Update each sub-agent tool description to document the `context`
  field."
- "Render `context` in the CLI thinking trace (extend `ThinkingEvent`
  with an optional `context` field on `tool_call` events)."

### Phase 3 — Per-(user, agent) history
- "Rekey `ChatHistoryManager` (or introduce `SubAgentHistoryManager`)
  by `(user_id, agent_name)`."
- "Wire `create_sub_agent` to load+persist its own history slice;
  decide what to store (full inner transcript vs trimmed pairs)."
- "Update `/clear` to wipe per-agent histories for the user; consider
  `/clear <agent>`."
- "Surface a `+N prior turns` hint in the CLI thinking trace."
- "Define and implement the per-agent trim policy (token-budgeted)."

### Phase 4 — Tuning
- "Per-agent memory policy switches: configurable in
  `create_sub_agent` rather than hard-coded."
- "Build a small replay/eval harness for delegation quality
  regression."
- "Tune per-agent window sizes and `context` length cap based on
  observed token costs."

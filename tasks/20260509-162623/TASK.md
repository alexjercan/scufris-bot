# Phase 1 — Prompt rework for Option F memory model

- STATUS: CLOSED
- PRIORITY: 75
- TAGS: prompts,agents,phase1

> Phase 1 of the Option F rollout from `tasks/20260509-154912/TASK.md`
> (the multi-agent memory design doc). Prompt-only changes: no code,
> no architecture, no schema. Ships value on its own and gives us a
> baseline to evaluate Phases 2/3 against.

## Why this is Phase 1

We agreed on Option F (per-agent history + Scufris-authored `context`
arg). Even before any of the architectural plumbing exists, we can
land the *prompt contract* that Option F implies — and several of its
benefits (clearer delegations, sub-agent refusal protocol, cleaner
sub-agent prompts) materialise immediately, with zero risk.

Phase 1 is a **pure prompt PR**. If we hate it, `git revert` is the
rollback.

## Scope

All changes live in `utils/agent_builder.py` (the constants
`MAIN_AGENT_PROMPT`, `CODING_AGENT_PROMPT`, `KNOWLEDGE_AGENT_PROMPT`,
`UTILITIES_AGENT_PROMPT`, `JOURNAL_AGENT_PROMPT`) plus the per-agent
tool descriptions on the `@tool`-wrapped sub-agents in
`create_sub_agent`.

Out of scope: code changes, schema changes, history rekeying,
`context` argument plumbing. Those are Phases 2 and 3.

## Deliverables

### 1. Rewrite Scufris main system prompt (`MAIN_AGENT_PROMPT`)

Add / make explicit:

- **Memory model statement.** "You are the only agent that
  remembers the user-facing conversation. Sub-agents have their own
  *private* memory of past calls *they* handled, but they do not see
  this conversation. When you delegate, phrase the request as a
  self-contained task and rely on your own memory to fill in any
  missing background."
- **Delegation contract.** No anaphora ("that one", "it", "the same
  thing") in tool calls. Every delegation must be intelligible to a
  cold reader who has only the sub-agent's prompt.
- **Forward reference to Phase 2.** A short note that future versions
  will accept a `context` field for explicit briefing — but for now
  the contract is "self-contained query".
- **Refusal handling (delegation-failure protocol from the design
  doc).** When a sub-agent returns `cannot_handle: <reason>`, follow
  the fallback ladder: try a more appropriate sub-agent → attempt the
  task itself if possible → tell the user honestly "I can't do that
  right now."
- **3–5 worked examples** of delegations: the user message, what
  Scufris remembers, and the well-formed tool call. At least one
  example showing rephrasing of an anaphoric follow-up
  ("…and Ploiesti?" → `knowledge_agent("weather forecast for Ploiesti
  for the next 3 days")`). At least one example showing a refusal
  re-route.

### 2. Add `## Memory & Context` section to each sub-agent prompt

Identical structure across all four (`CODING_`, `KNOWLEDGE_`,
`UTILITIES_`, `JOURNAL_`):

- "You are invoked as a tool by the main agent (Scufris). You do
  **not** see the user-facing conversation. The query you receive is
  the entire context Scufris chose to pass."
- "You currently have no persistent memory across calls. Treat each
  invocation as fresh." (Will be revised in Phase 3 for agents with
  history-on per the policy table.)
- "If the request is genuinely outside your competence (e.g. wrong
  domain, missing prerequisite info you can't ask for), return
  `cannot_handle: <one-line reason>` followed by any brief context
  that would help Scufris re-route. Do NOT guess. Do NOT invent
  facts. Do NOT ask follow-up questions — your only output channel is
  the tool result."

### 3. Slim-down pass on `JOURNAL_AGENT_PROMPT` (parallelisable)

The current journal prompt is ~150 lines. Goals:

- Extract anything that's restated boilerplate (also-true-of-other-
  agents content) and drop it (the new shared `## Memory & Context`
  section above replaces some of it).
- Tighten worked examples — keep the ones that demonstrably alter
  behaviour, drop the ones that just illustrate "use the tool".
- Target: ~50% length reduction without losing journal-specific
  semantics (date handling, append-vs-replace rules, daily-flow
  conventions). Exact size not a hard requirement — readability is.

### 4. Refactor: extract any other shared boilerplate

If, while doing (2) and (3), you notice the *same* paragraphs
appearing across multiple sub-agent prompts (other than the new
`## Memory & Context` block), factor them into a module-level
constant and reference it. Don't over-engineer — only do this when
the duplication is actual copy-paste, not "two prompts that happen to
make a similar point".

## Acceptance criteria

- [ ] `MAIN_AGENT_PROMPT` contains the memory-model statement, the
      delegation contract, the refusal-handling protocol, and at
      least 3 worked examples (one of which involves rephrasing an
      anaphoric follow-up; one of which involves handling a
      `cannot_handle` refusal).
- [ ] All four sub-agent prompts contain a `## Memory & Context`
      section with the wording from deliverable (2).
- [ ] All four sub-agent prompts document the `cannot_handle: ...`
      refusal format.
- [ ] `JOURNAL_AGENT_PROMPT` is materially shorter than today
      (eyeball check: it should look like the others in length, not
      twice as long) without behavioural regressions on the journal
      flows we already use.
- [ ] Manual smoke test: run `cli.py`, ask a question that involves
      a follow-up requiring rephrasing (e.g. "weather in Bucharest"
      then "and Ploiesti?"), confirm the second call to
      `knowledge_agent` arrives with a self-contained query in the
      thinking trace.
- [ ] Manual smoke test: ask `journal_agent` (via Scufris) to do
      something clearly out of its lane (e.g. "what's the weather
      tomorrow"), confirm a `cannot_handle` refusal and that Scufris
      re-routes to `knowledge_agent`.

## Notes / pitfalls

- **Don't promise the `context` arg in the sub-agent prompts yet** —
  Phase 2 hasn't shipped. If the prompt says "Scufris will brief you
  via `context`" but no such field exists, the LLM may hallucinate or
  complain. Mention it only in the main prompt's forward-reference
  paragraph.
- **Refusal is hard to drill into small models.** The `cannot_handle:`
  prefix is deliberate — it's a tag we can grep for in logs and
  eventually parse programmatically. Keep the format consistent
  across all four sub-agent prompts so we can build on it later.
- **Worked examples in the main prompt should look like real
  transcripts**, not synthetic illustrations. Pull from actual
  observed conversations where possible.
- **Don't over-edit `UTILITIES_AGENT_PROMPT`.** It's already terse;
  the only required addition is the `## Memory & Context` block.

## Out of scope (Phase 2 / 3 / 4)

- Adding the `context: str` parameter to sub-agent tools.
- Per-(user, agent) history.
- CLI thinking-trace changes (rendering `context`, showing prior-
  turn counts).
- Token-budget tuning.
- Eval harness.

## References

- Design doc: `tasks/20260509-154912/TASK.md` (Option F, the
  per-agent memory policy table, the delegation-failure protocol).
- Current prompts: `utils/agent_builder.py` lines ~40–287.
- Sub-agent wrapper: `utils/agent_builder.py:295` (`create_sub_agent`,
  for the tool descriptions to update alongside the prompts).

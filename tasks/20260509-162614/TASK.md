# History compaction spike: sliding window + summary + user-facts hashmap

- STATUS: OPEN
- PRIORITY: 20
- TAGS: spike,memory,research

> **Bookmark only — do not work on this yet.** Filed during the
> Option F design discussion (`tasks/20260509-154912/TASK.md`,
> Decisions #2) so we don't lose the idea. Pick this up only after
> Phases 1–3 of Option F have shipped and we have a real signal on
> where the current sliding window starts to hurt.

## Motivation

Today `ChatHistoryManager` is a flat sliding window: keep the last N
messages per user, drop the rest. Once Phase 3 of Option F lands,
each sub-agent gets the same treatment per `(user, agent)` slice.

The sliding window is fine as a starting point but has known weak
spots:

- Anything that falls out of the window is gone — even if it's a
  durable fact about the user (preferences, location, ongoing
  projects, dietary stuff for journal, etc.).
- The window grows linearly with conversation length until it hits
  the cap, then truncates abruptly. No graceful degradation.
- For sub-agents (Phase 3), full inner transcripts blow the window
  budget faster than for the main agent.

## Spike goals

Explore — don't commit to — a compaction strategy roughly along
these lines:

1. **Sliding window stays** as the freshness layer (most recent
   K messages verbatim).
2. **Summarisation layer**: when messages fall out of the window,
   they get folded into a running summary blob — one per
   `(user, agent)` slice. Cheap LLM call (small model) on
   eviction, batched.
3. **User-facts hashmap**: a structured side-channel of *durable*
   facts extracted from the conversation ("user lives in Bucharest",
   "user is vegetarian", "user's kid's name is X"). Injected into the
   prompt as a small key-value block. Updated by an even-cheaper
   extraction pass (or by a dedicated tool the agents can call to
   `remember(fact)`).

The combined "memory" presented to an agent then becomes:

```
[ system_prompt,
  {role: "system", content: "Known user facts: {...}"},
  {role: "system", content: "Earlier conversation summary: ..."},
  ...sliding window of last K messages... ]
```

## Open questions to investigate

- Who triggers the summarisation pass? On eviction (synchronous,
  adds latency to the turn that bumps a message out)? On idle
  (background, requires an event loop)? Periodically (cron-like)?
- Same question for user-fact extraction. Probably cheaper to
  piggyback on the existing turn rather than schedule.
- Storage format. Per-`(user, agent)` JSON blob? Single
  per-user store with agent-scoped namespaces? Disk vs in-memory?
- Conflict / staleness on user facts. What if "user lives in
  Bucharest" and "user just moved to Cluj" both exist? Last-write-
  wins is naive but probably fine at single-user scale.
- Cost. Summarisation calls aren't free even with a small model.
  Need to bound them — e.g. only summarise when window has actually
  evicted something, batch evictions.
- Privacy / hygiene. The user-facts hashmap is the most permanent
  store we have. Need a `/forget <fact>` story (or at minimum,
  inspectability via a CLI command).

## Why this is low priority right now

- We don't yet have Phase 3 (per-agent history) running. Until then
  there's nothing for compaction to compact except the main
  conversation, which currently fits comfortably.
- Picking the right compaction shape is much easier with real usage
  data — what kinds of facts actually get lost, what kinds of
  long-tail context the user actually expects to be remembered.
- The simpler "tune window sizes and the `context` cap" knobs from
  Phase 4 may be enough for a long time.

## Definition of "done" for this spike (when picked up)

A short writeup (in this file) covering:

- Recommended approach (or "don't do this, here's why").
- Concrete data structures + storage location.
- Where in the code each piece would hook in
  (`ChatHistoryManager`, `create_sub_agent`, etc.).
- Estimated implementation cost (small / medium / large).
- One or two adversarial scenarios stress-testing the design.

Then file the actual implementation as a separate task with a
proper rollout plan.

## References

- Parent design doc: `tasks/20260509-154912/TASK.md` (Decisions #2,
  Deferred section).
- Current implementation: `utils/history.py` (`ChatHistoryManager`).

---

## Findings (2026-05-09)

> Drafted by Claude after a scoped Q&A. Three forks were locked
> before writing: (a) writeup-only scope, no code; (b) facts populated
> via *both* an explicit `remember(...)` tool *and* opportunistic
> auto-extraction; (c) the main agent (`scufris`) gets the same
> three-layer stack as sub-agents (symmetric model — `scufris` is
> already just another `(user, agent)` slice in `ChatHistoryManager`).

### Recommended approach: do it, in four phases

The three-layer model from the original spike goals stands up under
scrutiny. Concretely:

```
Layer 1  Sliding window         (already in place — keep as-is)
Layer 2  Running summary        (one per (user, agent) slice)
Layer 3  Facts hashmap          (one per (user, agent) slice)
```

Both new layers live inside `ChatHistoryManager` next to the existing
`_histories` dict, share the same `(user_id, agent)` key, and are
populated by a single **compactor pass** that fires on eviction. The
compactor is one cheap LLM call that returns *both* the updated
summary and any new facts in one shot — no separate
summarisation+extraction calls.

### Architecture overview

```
                 ┌─────────────────────────────────────────┐
                 │          ChatHistoryManager             │
                 │                                         │
                 │  _histories : List[BaseMessage]         │  (window)
                 │  _summaries : str                       │  (summary)
                 │  _facts     : Dict[str, str]            │  (hashmap)
                 │                                         │
                 │   add_messages → _trim_by_tokens →      │
                 │     evicted msgs ─┐                     │
                 │                   ▼                     │
                 │             ┌──────────┐                │
                 │             │ compactor│ ── LLM call    │
                 │             └──────────┘                │
                 │     summary, new_facts ◀──┘             │
                 └─────────────────────────────────────────┘

When assembling messages for an agent:

  [ system_prompt,
    SystemMessage("Known facts: {k:v, ...}"),         ← from _facts
    SystemMessage("Earlier summary: ..."),            ← from _summaries
    ...sliding window from _histories...,
    HumanMessage(current_query) ]
```

Privacy boundary unchanged: each `(user, agent)` slice is private to
that agent. `_facts[(uid, "knowledge_agent")]` is invisible to
`coding_agent`.

### Data structures

Added to `ChatHistoryManager`:

```python
# Per-(user, agent) running summary, char-budgeted (hard cap).
self._summaries: Dict[Tuple[int, str], str] = defaultdict(str)

# Per-(user, agent) facts. Key = short slot name (e.g. "location"),
# value = fact statement. Last-write-wins on key collision.
self._facts: Dict[Tuple[int, str], Dict[str, str]] = (
    defaultdict(dict)
)
```

Why `Dict[str, str]` instead of `List[str]`:

- Slot keys give the auto-extractor and `remember(...)` tool a
  natural merge primitive (overwrite vs append).
- Easy `forget(key)`.
- Trivial JSON serialisation if we ever add persistence.
- Risk: extractor invents new keys for the same concept
  (`home_city` vs `location`). Mitigation: prompt the extractor with
  the *existing* facts dict and instruct it to reuse keys.
  Fallback: occasional GC pass (Phase 4).

Hard caps:
- Summary: ~1500 chars (~375 tokens). Compactor must compress, not
  just append.
- Facts: ~20 entries per slice. Beyond that, drop oldest by
  insertion order (LRU on writes).

### Compactor design

New module `utils/memory_compactor.py`:

```python
class CompactionResult(TypedDict):
    summary: str               # full updated summary, not a delta
    facts: Dict[str, str]      # facts to merge (last-write-wins)

class Compactor(Protocol):
    def compact(
        self,
        evicted: List[BaseMessage],
        existing_summary: str,
        existing_facts: Dict[str, str],
    ) -> CompactionResult: ...

class LLMCompactor:           # default, prod
    def __init__(self, llm): ...

class NoopCompactor:          # tests + opt-out
    def compact(self, *_a, **_kw) -> CompactionResult:
        return {"summary": "", "facts": {}}
```

Single-prompt template (sketch):

```
You are a memory compactor. Given evicted messages from a chat,
update a running summary and extract durable user facts.

Existing summary (max 1500 chars, you may rewrite/compress):
{existing_summary}

Existing facts (reuse keys when applicable):
{existing_facts as YAML}

Evicted messages:
{evicted as plain dialogue}

Return strict JSON:
{"summary": "...", "facts": {"slot": "value", ...}}

Rules: facts are durable user attributes only — preferences,
identity, locations, ongoing projects. NOT transient state
("user is asking about X right now"). If unsure, omit.
```

LLM choice: same small model as `utilities_agent` (cheap, local
Ollama). Failure mode: if the call errors or returns malformed JSON,
log a warning and skip — eviction still proceeds, we just don't
update summary/facts that round. **The window is the source of
truth; summary/facts are best-effort enrichment.**

### Trigger / timing

**v1: synchronous on eviction.** When `_trim_by_tokens` (or
`_trim_history`) is about to drop messages, it instead:

1. Captures the to-be-evicted messages.
2. Calls `compactor.compact(evicted, existing_summary, existing_facts)`.
3. Merges the result into `_summaries` and `_facts`.
4. Drops the messages from `_histories`.

Trade-off: this adds compactor latency to the turn that triggers
eviction (the user pays for compaction inline). Acceptable for v1
because eviction is rare-ish and compactor LLM is small/fast.

**Phase 4: deferred async** (future). Fire-and-forget post-turn,
using `asyncio.create_task` in the bot path and a worker thread in
the CLI path. Complicates testing and error reporting; defer until
sync latency is observed to hurt.

### Tools: `remember` / `forget`

Two new LangChain tools available to **every** agent:

```python
@tool
def remember(key: str, value: str) -> str:
    """Record a durable fact about the user under `key`."""
    # Routed via the agent's name + user_id (RunnableConfig)
    # to history_manager.add_facts(user_id, agent, {key: value}).

@tool
def forget(key: str) -> str:
    """Remove a previously remembered fact under `key`."""
```

- They write to *the calling agent's* slice, identified the same way
  per-agent history is identified today (via `RunnableConfig` →
  `agent_name`).
- The agent prompt must explicitly authorise their use ("if the user
  states a durable preference, call `remember`; if they correct or
  retract one, call `forget`").
- Auto-extraction (compactor pass) is the safety net: covers facts
  the agent didn't bother to record.

### Code hook points

1. **`utils/history.py`**
   - Add `_summaries`, `_facts` dicts.
   - Modify `_trim_by_tokens` (and `_trim_history`) to capture
     evicted messages and invoke the compactor before dropping them.
   - Add `add_facts(user_id, agent, facts)`,
     `remove_fact(user_id, agent, key)`,
     `get_compaction(user_id, agent) -> (str, Dict[str, str])`.
   - Modify `get_history_with_new_message` to optionally prepend
     summary + facts as SystemMessages.
   - Wipe `_summaries` and `_facts` on `clear_user`.

2. **`utils/memory_compactor.py`** (new)
   - `Compactor` protocol + `LLMCompactor` + `NoopCompactor`.
   - Module-level `create_compactor(llm)` factory.

3. **`utils/agent_builder.py`** / **`utils/sub_agent.py`**
   - Inject `remember` / `forget` into the per-agent toolset.
   - When loading per-agent history for invocation, also load
     summary + facts via `get_compaction(...)`.

4. **`main.py`** / **`cli.py`** (bootstrap)
   - Pass a `Compactor` instance into `create_history_manager`.

5. **CLI thinking trace**
   - On compaction events, emit a `ThinkingEvent.compaction` (new
     variant) with `(agent, n_evicted, n_new_facts)` for visibility.

### Implementation cost

**Medium.** Rough breakdown:

| Piece                                | Size  |
|--------------------------------------|-------|
| `_summaries` / `_facts` storage      | small |
| Compactor protocol + Noop            | small |
| LLMCompactor + prompt                | medium (iterative tuning) |
| Trim path eviction → compactor wire  | medium (delicate) |
| `remember` / `forget` tools          | small |
| Sub-agent assembly: prepend layers   | small |
| Bootstrap wiring                     | small |
| `/clear` covers new dicts            | trivial |
| Thinking-trace `compaction` event    | small |
| Tests (compactor mocked at boundary) | medium |

Total: ~2–3 focused days. Phaseable into 3 PR-sized chunks (see
implementation plan below).

### Adversarial scenarios

**1. Runaway summary growth.** Compactor naively appends new
content each eviction; after 200 evictions the summary alone is
10k tokens.

- *Mitigation*: hard char cap (1500) enforced inside the compactor
  prompt AND clipped post-hoc in `add_facts`/`add_summary`.
- *Stress test*: simulate 500 sequential evictions of varied
  content, assert `len(summary) <= 1500` throughout.

**2. Conflicting facts across slices.** User says
"I live in Bucharest" while talking to `knowledge_agent` (saved as
`location: Bucharest` in that slice). Two weeks later they tell
`journal_agent` "I'm in Cluj now" (saved as `location: Cluj` in
journal's slice). Knowledge still thinks Bucharest.

- *Acknowledged design cost*: facts are per-agent, so they can
  drift. Matches the parent design's privacy boundary.
- *Mitigation*: the main agent (`scufris`) carries authoritative
  user facts; sub-agents should treat their own `_facts` as a
  cache, not ground truth. Prompts must emphasise this.
- *Future*: a `/sync` command or a periodic "pull from main" pass
  on agent invocation. Out of scope for v1.

**3. `forget` doesn't propagate.** User says "forget where I live"
to Scufris. Scufris calls `forget("location")` on its own slice.
Sub-agents still have it. User is surprised.

- *Mitigation*: `forget` invoked from the main agent fans out to
  every `(user, *)` slice with that key. Sub-agent `forget` calls
  stay local.
- Alternative: a dedicated `/forget <key>` CLI command that wipes
  across slices and sidesteps the agent.

**4. Compactor LLM corrupts a fact.** Auto-extractor reads "user
hates broccoli" and confidently writes `diet: broccoli`. Now the
agent thinks the user *prefers* broccoli.

- *Mitigation*: compactor prompt is conservative ("if unsure,
  omit"); facts that come from auto-extraction get a soft
  provenance marker (`origin: "auto"` vs `origin: "tool"`),
  visible in `/stats`. User can `/forget` directly. Not a v1
  blocker — this is a quality-not-correctness issue.

**5. Compactor LLM is down / errors.** Eviction must still happen
(or the window grows unbounded).

- *Mitigation*: compactor errors are caught, logged, and ignored.
  Eviction proceeds without updating summary/facts. The window
  remains the source of truth.

### Open questions deferred to implementation

These don't block the design — pick reasonable defaults, dogfood,
adjust:

- Char cap for summary (suggest 1500).
- Max facts per slice (suggest 20).
- Compactor model (suggest same as `utilities_agent`).
- Whether `remember` is exposed as a tool to the main agent or only
  to sub-agents (suggest: all agents — symmetry).
- JSON persistence of summary/facts across process restarts
  (suggest: not in v1; honour parent design's "no persistence"
  non-goal).

### Implementation plan (proposed task split)

Three PR-sized tasks, each independently shippable:

1. **Compactor scaffolding + storage.**
   `utils/memory_compactor.py` (Protocol + Noop only — no LLM yet),
   `_summaries`/`_facts` in `ChatHistoryManager`, eviction wires
   into Noop compactor. Behaviour-preserving: with Noop, history
   acts exactly as today. Tests: storage round-trip, eviction
   triggers compactor, `/clear` wipes new dicts.
   *Cost: small.*

2. **LLMCompactor + summary/facts injection.**
   Real `LLMCompactor` with the Ollama-backed prompt. Modify
   `get_history_with_new_message` and `sub_agent_tool` to prepend
   summary + facts as SystemMessages. Bootstrap wires
   `LLMCompactor` into `create_history_manager`. Tests mock the LLM
   at the SDK boundary.
   *Cost: medium (prompt tuning is the hard part).*

3. **`remember` / `forget` tools + thinking-trace polish.**
   Add the tools, wire them into `agent_builder`. Add
   `ThinkingEvent.compaction` and `/stats` columns for summary
   length + fact count. Update agent prompts to authorise the new
   tools.
   *Cost: small-medium.*

Phase 4 (out of v1 scope, file only when needed):
- Deferred async compaction.
- Fact GC / dedup pass.
- JSON persistence.
- Cross-slice fact propagation (`forget` fan-out from main).

### Acceptance criteria for *this spike*

- [x] Recommended approach documented (3-layer stack).
- [x] Concrete data structures + storage location specified.
- [x] Code hook points enumerated.
- [x] Implementation cost estimated (medium, 2–3 days).
- [x] At least two adversarial scenarios stress-tested (five
      documented above).
- [ ] User reviews findings and either approves the implementation
      plan, requests revisions, or rejects with reasoning.
- [ ] On approval: file the three implementation tasks, then mark
      this spike `STATUS: CLOSED`.


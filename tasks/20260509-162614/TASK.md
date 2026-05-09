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

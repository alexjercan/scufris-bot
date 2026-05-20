# Persistent user facts and per-turn agent scratchpad

- STATUS: OPEN
- PRIORITY: 0
- TAGS: memory,persistence,backlog

## The Distinction

- **User facts** (`_facts`): durable attributes about the person — location, dietary preferences, ongoing projects, preferences. Should survive restarts. Should be queryable.
- **Agent scratchpad**: ephemeral working notes within a single conversation — "I just checked the weather, no need to call again". Can stay in-memory.

## Facts Persistence

- The `facts` table (from the conversation persistence task) is exactly this. `add_facts` writes to SQLite; `get_facts` reads from it. The in-memory dict becomes a write-through cache.
- `/stats` shows fact count per agent slice
- **New CLI command**: `/facts [agent]` — lists all stored facts for the user, optionally filtered by agent. Lets you inspect what Scufris "knows" about you.
- `/forget <key>` becomes a durable delete from the DB

## Scratchpad (new concept)

- A per-turn, per-agent ephemeral key-value store, initialized at the start of each `sub_agent_tool` call and discarded after
- Useful for agents that need to carry state within a multi-step tool chain but don't want to pollute `_facts` with transient data
- Example: journal agent sets `scratchpad["today_entry_created"] = True` early in a turn so it doesn't call `today_create_tool` twice
- Implementation: just a dict passed into the sub-agent invocation via the composed user message or a `ContextVar` — nothing persisted

## Combined Picture

Scufris memory layers (top = most ephemeral):

```
scratchpad    — per-turn, per-agent, in-memory only
window        — last N messages, SQLite-backed
summary       — compressed older turns, SQLite
facts         — durable user attributes, SQLite, cross-restart
RAG store     — documents + past conversations, vector DB
```

## Dependencies on User Identity (`20260520-145231`)

**Hard** dependency for facts; **none** for scratchpad.

### Facts
- Schema: `facts(user_id REFERENCES users(id), agent, key, value, source, ts)` — `user_id` is the unified internal ID from the identity layer
- Without identity unification, facts learned in Telegram won't be visible to the CLI session (because they're stored under different surface-local user IDs) — this defeats the entire "Scufris remembers you" value proposition
- `/facts` and `/forget` commands operate on the unified user, so changes made in either surface are immediately visible in the other
- **Sequencing (decided)**: identity layer ships first, then this task. No `user_id=1` interim version.

### Scratchpad
- Pure per-turn, in-memory, lives entirely inside one agent invocation — never touches the DB
- Doesn't need identity at all beyond knowing which user's turn is currently executing (which is already plumbed through every tool call)

## Fact Categories

Worth distinguishing in the schema so the UX can group them:

| Category | Examples | Source |
|---|---|---|
| `profile` | name, timezone, location, dietary restrictions | user-declared, rarely changes |
| `preference` | "prefers metric units", "no emojis please" | inferred from feedback |
| `state` | "currently learning Rust", "on cut until June" | medium-lived, agent-managed |
| `relation` | "Maria is my partner", "the cat is named Felix" | user-declared, contextual |

Add a `category` column to the `facts` table for filtering and a `/facts profile` shortcut.

## Conflict Resolution

Facts can contradict each other over time. Two options:
- **Append-only, latest wins**: keep all rows, query returns most recent per `(user_id, agent, key)`. History preserved, easy.
- **Overwrite**: `UPSERT` on `(user_id, agent, key)`. Simpler queries, loses history.

Recommend **append-only** — disk is cheap and audit trail helps debug "why does Scufris think I live in Cluj?".

## Complexity Estimate

Small-to-medium. Facts table + CRUD + CLI commands is ~2 days once the persistence task lands (they share the schema). Scratchpad is half a day. The harder, unscoped work is **fact extraction** — having an agent actually decide what's worth promoting from a conversation to a durable fact. That's a separate task worth filing.

## Fact Extraction (decided)

**v1 uses explicit `remember_fact_tool` calls only.** Agents call the tool when they decide something is durable. Predictable, debuggable, no silent surprises in the facts table. Passive end-of-turn extraction is deferred to a later task once the explicit path is solid and we have real usage data on what gets remembered.

Tool signature:

```python
remember_fact_tool(key: str, value: str, category: str, source_msg_id: int | None = None)
# category: "profile" | "preference" | "state" | "relation"
```

Each agent's system prompt gets a `## Memory` section listing example situations where it should call the tool ("when the user states their dietary preference", "when they tell you a recurring schedule", etc.).

## Open Questions

- **Fact extraction**: who decides what becomes a fact — explicit `remember_fact_tool` calls only, or a passive extraction pass at end-of-turn? Passive is magical but error-prone. *(see Decided below)*
- **Fact namespacing**: should the `agent` column be a hard partition (journal facts hidden from coding agent) or just a tag (all agents see all facts)? My instinct: tag, not partition.
- **TTL on `state` facts**: should "currently learning Rust" auto-expire after N days if not reconfirmed? Probably yes, but adds policy.
- **Source attribution**: `source` column could be the message ID that produced the fact — useful for "where did you learn that?" debugging. Worth doing from day one.
- **Scratchpad scope**: per-turn or per-multi-step-tool-chain? If the journal agent calls 5 tools in one turn, does scratchpad persist across all 5? (Yes, almost certainly.)

## Decided

- **Sequencing**: identity layer (`20260520-145231`) ships before this task
- **Extraction**: explicit `remember_fact_tool` only for v1; passive extraction deferred

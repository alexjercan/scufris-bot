# Persistent conversation store: SQLite backend and export

- STATUS: OPEN
- PRIORITY: 0
- TAGS: memory,persistence,cli,backlog

## The Shift

`ChatHistoryManager` currently lives entirely in-process (dict of lists). Replace the backing store with SQLite — same API surface, persistent across restarts.

### Schema

```sql
messages(id, user_id, agent, role, content, tool_calls_json, ts)
summaries(user_id, agent, summary, updated_at)
facts(user_id, agent, key, value, source, ts)
```

- On startup, `ChatHistoryManager` loads the window from SQLite; on write, it appends a row
- Trim still happens in-memory for speed but is also reflected in the DB (soft-delete or archive old rows)
- **Migration**: existing in-memory state is ephemeral so no migration needed — first restart starts fresh from the DB

## Export

- `GET /v1/export?user_id=N&format=md|json` endpoint
- **Markdown export**: one `##` heading per turn, assistant responses rendered as-is, tool calls shown as `> called weather("Ploiesti")`
- **JSON export**: raw message list with metadata, importable back in
- `/export` CLI slash command saves to `~/.scufris/exports/<timestamp>.md`

## RAG Reuse

- The `messages` table is also a natural corpus for the RAG store — past conversations are documents worth indexing
- A sync source `type = "conversations"` in `rag_sources.toml` would chunk and embed old turns, letting you ask "what did we figure out last week about the OpenCode issue?"

## Dependencies on User Identity (`20260520-145231`)

**Hard** dependency. The schema is keyed by `user_id` everywhere — and that ID must be the unified internal one from the identity layer, not surface-local IDs (CLI=1, Telegram=123456789), or you fragment the same user's history across phantom accounts.

### Schema additions for identity integration

```sql
-- foreign key to users table from identity layer
messages(
  id, user_id REFERENCES users(id), agent, role,
  content, tool_calls_json, surface, ts
)
-- new column: surface ("cli" | "telegram" | "web") so /stats can break down by origin
-- and the CLI can optionally show [telegram] tags on shared history
```

- **Migration path**: if this task ships *before* identity, write to `user_id` as the surface-local integer and rewrite via a one-shot migration once identity lands (`UPDATE messages SET user_id = (SELECT id FROM surface_bindings WHERE surface = 'cli' AND surface_id = '1')`). Annoying but tractable.
- **Sequencing (decided)**: identity layer (`20260520-145231`) lands first — no migration needed.
- **`/clear` semantics**: clears history for the unified `user_id`, so messages from both CLI and Telegram disappear together (matches the cross-surface-continuity promise from the identity task).
- **Export**: `GET /v1/export?user_id=N` returns the full unified history, with the new `surface` column visible in JSON exports so the user can see which channel each message came from.

## Retention and Privacy

- **No retention cap by default** — disk is cheap and the user benefits from long memory. Add `[user.history] max_age_days = 365` config knob later if needed.
- **Hard delete** via `/forget-history --before YYYY-MM-DD` (CLI only, requires confirmation). Soft delete by default for safety.
- **Encryption at rest**: out of scope for v1, but the SQLite file should at least live under `$XDG_DATA_HOME/scufris/` with 0600 perms.

## Complexity Estimate

Medium. The SQLite schema + write-through cache + window loader is ~3 days. Export endpoints + CLI command another 1–2 days. Wiring it through `ChatHistoryManager` without breaking the existing in-memory API is the trickiest part — needs careful testing.

## Open Questions

- **`tool_calls_json` shape**: do you want full LangChain `AIMessage.tool_calls` serialization, or a slim `{name, args, result_preview}` representation? Full is debuggable, slim is portable.
- **Summary regeneration**: when do summaries get updated — every N new messages, on each turn, or lazily before retrieval?
- **Multi-window per agent**: each sub-agent currently has its own conversation slice. The schema supports this via the `agent` column, but do you want a "unified view" mode for export, or strictly per-agent?
- **Import path**: do you want round-trip import of JSON exports (useful for backups / moving machines)? Adds complexity but minor.
- **Search-as-you-go**: should `/history search <query>` use the RAG embeddings or simple LIKE / FTS5? FTS5 is built into SQLite and probably enough for v1.

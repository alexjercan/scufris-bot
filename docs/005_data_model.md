# 005 — Data model

The SQLite schema, the migration mechanism, the connection model,
and what scufris stores vs. what opencode stores.

## What's in the DB

Path: `$SCUFRIS_STATE_DIR/scufris.sqlite` (default
`~/.local/state/scufris/scufris.sqlite`). Single file. Created on
first boot if missing.

Tables (initial schema in `migrations/001_initial.sql`):

```
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│   users ◄───────────┐                                              │
│     id              │                                              │
│     username (UQ)   │                                              │
│     created_at      │                                              │
│                     │                                              │
│   surface_bindings ─┘                                              │
│     user_id         (FK→users.id)                                  │
│     surface         ┐                                              │
│     surface_id      ├── PRIMARY KEY (surface, surface_id)          │
│                                                                     │
│   channels ◄────────┐                                              │
│     id              │                                              │
│     user_id (FK)    │                                              │
│     surface         │                                              │
│     surface_id      │── UNIQUE (user_id, surface, surface_id, agent)│
│     agent           │                                              │
│                     │                                              │
│   session_links ────┘                                              │
│     channel_id (FK→channels.id, PK)                                │
│     oc_session_id                                                  │
│     created_at                                                     │
│     last_used_at                                                   │
│                                                                     │
│   facts                                                            │
│     id                                                             │
│     user_id (FK)    ┐                                              │
│     key             ├── UNIQUE (user_id, key)                      │
│     value                                                          │
│     created_at                                                     │
│     updated_at                                                     │
│                                                                     │
│   event_log                                                        │
│     id                                                             │
│     ts                                                             │
│     oc_session_id                                                  │
│     event_type                                                     │
│     payload_json                                                   │
│                                                                     │
│   _schema_migrations                                               │
│     filename (PK)                                                  │
│     applied_at                                                     │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

What lives here vs. what lives in opencode:

| Concept | Owner |
|---------|-------|
| Conversation content (messages, tool calls, tool results) | opencode (its own session store) |
| Compaction summaries | opencode |
| Token / cost telemetry | opencode (per-message `info`); scufris will aggregate via `#13` |
| Identity (`users`, `surface_bindings`) | scufris |
| Channel ↔ session pointers (`channels`, `session_links`) | scufris |
| Per-user durable facts | scufris (table exists; tooling in `#19`) |
| Audit log of opencode events | scufris (table exists; consumer in `#11`) |

We deliberately don't persist conversation content. opencode owns
that; we own *pointers* to opencode's sessions plus our identity
layer.

## Tables

### `users`

```sql
CREATE TABLE users (
  id          INTEGER PRIMARY KEY,
  username    TEXT UNIQUE NOT NULL,
  created_at  INTEGER NOT NULL          -- unix seconds
);
```

| Column | Type | Constraints | Meaning |
|--------|------|-------------|---------|
| `id` | INTEGER | PK | Canonical user identifier. `1` is the seeded default user. |
| `username` | TEXT | UNIQUE NOT NULL | Display name. Sourced from `[user] username = "..."` in `config.toml`. |
| `created_at` | INTEGER | NOT NULL | Unix seconds when the row was inserted. |

Inserted by:

- `app._seed_default_user()` (`app.py:95`) — `(1, "default", <now>)`,
  `INSERT OR IGNORE`.
- `identity._ensure_user_row()` (`identity.py:242`) — `(<auto>,
  <toml-username>, <now>)` on first TOML hit.

Read by:

- `app._validate_identity_override()` (`app.py:111`) — checking
  `SCUFRIS_USER_ID`.
- `identity._username_for()` (`identity.py:234`) — every `resolve_user`
  call.

Never updated. Never deleted. (If you want to delete a user, you have
to clean up `surface_bindings`, `channels`, `session_links`, `facts`
first; foreign-key enforcement is on.)

### `surface_bindings`

```sql
CREATE TABLE surface_bindings (
  user_id     INTEGER NOT NULL REFERENCES users(id),
  surface     TEXT NOT NULL,
  surface_id  TEXT NOT NULL,
  PRIMARY KEY (surface, surface_id)
);
```

| Column | Type | Constraints | Meaning |
|--------|------|-------------|---------|
| `user_id` | INTEGER | NOT NULL, FK → `users.id` | Who this surface belongs to. |
| `surface` | TEXT | NOT NULL, part of PK | `"cli" \| "telegram" \| "web" \| ...` |
| `surface_id` | TEXT | NOT NULL, part of PK | `"alex"`, `"8231376426"`, … |

The PRIMARY KEY is `(surface, surface_id)` — a surface_id can only
belong to one user at a time. This is the cache that step 2 of the
identity resolver hits.

Inserted by `identity._ensure_surface_binding()` (`identity.py:272`).
Idempotent — double-insert is a no-op.

The override path (`SCUFRIS_USER_ID`) does *not* write to this table.
Only TOML hits and default-fallback resolves do.

Sticky — once written, never updated. To re-route a surface_id, the
operator must `DELETE` and let the next call re-resolve.

### `channels`

```sql
CREATE TABLE channels (
  id          INTEGER PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id),
  surface     TEXT NOT NULL,
  surface_id  TEXT NOT NULL,
  agent       TEXT NOT NULL,
  UNIQUE (user_id, surface, surface_id, agent)
);
```

| Column | Type | Constraints | Meaning |
|--------|------|-------------|---------|
| `id` | INTEGER | PK | Channel identifier. Used by `/v1/sessions/{channel_id}/clear` and (future, `#33`) `/v1/sessions/{channel_id}/fork`. |
| `user_id` | INTEGER | NOT NULL, FK | Who's on this channel. |
| `surface` | TEXT | NOT NULL | Same vocabulary as `surface_bindings`. |
| `surface_id` | TEXT | NOT NULL | The per-surface user id for the channel. |
| `agent` | TEXT | NOT NULL | Which scufris agent persona handles this channel. |

The `UNIQUE (user_id, surface, surface_id, agent)` constraint is the
session-reuse key. Two `/v1/chat` calls with the same `(user_id,
surface, surface_id, agent)` always join the same `channels` row, and
therefore the same `session_links` row, and therefore the same
opencode session.

Different `agent` under the same `(user_id, surface, surface_id)`
means a new channel — the build agent and the plan agent don't share
context.

Inserted by `sessions.create_channel_link()` (`sessions.py:175`) on
the first chat for a new triple. Read by `sessions.list_user_channels`
(GET `/v1/sessions`) and `sessions.get_channel`. Never updated;
deletion happens only when the parent `users` row is deleted (which
isn't supported today).

### `session_links`

```sql
CREATE TABLE session_links (
  channel_id     INTEGER PRIMARY KEY REFERENCES channels(id),
  oc_session_id  TEXT NOT NULL,
  created_at     INTEGER NOT NULL,
  last_used_at   INTEGER NOT NULL
);
```

| Column | Type | Constraints | Meaning |
|--------|------|-------------|---------|
| `channel_id` | INTEGER | PK, FK | Same row as `channels.id`. 1:1 with channels. |
| `oc_session_id` | TEXT | NOT NULL | Opencode's session id (`ses_...`). |
| `created_at` | INTEGER | NOT NULL | Unix seconds when the link was created. |
| `last_used_at` | INTEGER | NOT NULL | Unix seconds — bumped on every reuse. |

The `channel_id` PK means a `channels` row has at most one
`session_links` row. The 1:1 cardinality is enforced by the schema.

Inserted by `sessions.create_channel_link()` (`sessions.py:175`)
alongside the `channels` row, in one transaction. Updated by
`chat._touch_session()` (`routes/chat.py:130`) on reuse. Deleted by
`sessions.clear_channel_link()` and `sessions.clear_user_links()`,
which back the live `POST /v1/sessions/{channel_id}/clear` and
`POST /v1/clear` endpoints (`#10`,
[`002_api_reference.md`](002_api_reference.md)).

Note that *the opencode session itself* persists when a `session_links`
row is deleted — only the *pointer* goes away. The session can be
re-bound if you know the `oc_session_id`. This is the design: scufris
should never destroy opencode's data.

### `facts`

```sql
CREATE TABLE facts (
  id           INTEGER PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id),
  key          TEXT NOT NULL,
  value        TEXT NOT NULL,
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL,
  UNIQUE (user_id, key)
);
```

| Column | Type | Constraints | Meaning |
|--------|------|-------------|---------|
| `id` | INTEGER | PK | Fact id. |
| `user_id` | INTEGER | NOT NULL, FK | Whose fact. |
| `key` | TEXT | NOT NULL, part of UQ | Free-form key. v0 has no schema; `#19` will refine. |
| `value` | TEXT | NOT NULL | Free-form value. Plain text. |
| `created_at` | INTEGER | NOT NULL | Unix seconds. |
| `updated_at` | INTEGER | NOT NULL | Unix seconds, bumped on `ON CONFLICT DO UPDATE`. |

Per-user durable facts. The "long-term memory" surface for the LLM
agent. v0 isn't writing or reading this table yet — `#19` will land
the `remember` / `forget` tools and the compaction-time injection
that uses them.

The `UNIQUE (user_id, key)` constraint is what makes concurrent
`remember` calls from multiple opencode worker contexts safe:
SQLite WAL + `ON CONFLICT (user_id, key) DO UPDATE` means last
writer wins on the same key, independent keys never collide.

### `event_log`

```sql
CREATE TABLE event_log (
  id            INTEGER PRIMARY KEY,
  ts            INTEGER NOT NULL,       -- unix millis
  oc_session_id TEXT,
  event_type    TEXT NOT NULL,
  payload_json  TEXT NOT NULL
);
```

| Column | Type | Constraints | Meaning |
|--------|------|-------------|---------|
| `id` | INTEGER | PK | Auto. |
| `ts` | INTEGER | NOT NULL | Unix **milliseconds** (different from the others — events are fine-grained). |
| `oc_session_id` | TEXT | nullable | Which opencode session the event belongs to, when known. |
| `event_type` | TEXT | NOT NULL | The `type` field from opencode's SSE event (e.g. `"message.part.delta"`). |
| `payload_json` | TEXT | NOT NULL | The full event body as serialised JSON. |

Audit log of opencode events we've seen. v0 isn't writing this table
yet — `#11` will land the SSE consumer that fills it. The plan is to
cap by row count or age; not the source of truth for conversation
content.

`oc_session_id` is nullable because opencode emits some
session-independent events (`server.connected`).

### `_schema_migrations`

```sql
CREATE TABLE IF NOT EXISTS _schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at INTEGER NOT NULL
)
```

Auto-created by `store.apply_migrations()` (`store.py:91`). One row
per applied migration file.

| Column | Type | Constraints | Meaning |
|--------|------|-------------|---------|
| `filename` | TEXT | PK | Just the filename (`001_initial.sql`), not the full path. |
| `applied_at` | INTEGER | NOT NULL | Unix seconds, set via `strftime('%s','now')` so the same SQL works across host clocks. |

Read at lifespan boot to determine which `migrations/*.sql` files are
new. Inserted in the same transaction as each migration's DDL — so a
crash mid-migration leaves both the DDL *and* the tracking row
absent, and the migration runs again on the next boot.

## Connection model

Defined in `store.py`. Three things matter:

1. **One connection per request.** FastAPI dependency `get_db_conn`
   (`dependencies.py:44`) opens a fresh connection at request start
   and closes it at request end via the `with connect(settings) as
   conn` context manager.

2. **Connections are not shared across threads or async tasks.**
   sqlite3 defaults to `check_same_thread=True`, and
   FastAPI's threadpool would mean a sync-deps connection couldn't
   legally be used by an async handler. Going async-only sidesteps
   that — see `001_architecture.md` "Dependency injection" for the
   reasoning.

3. **WAL + foreign-keys + autocommit-off.** Every connection runs
   the same PRAGMA setup in `_open()` (`store.py:56`):

   ```python
   conn = sqlite3.connect(path, autocommit=True)
   conn.row_factory = sqlite3.Row
   conn.execute("PRAGMA journal_mode = WAL")
   conn.execute("PRAGMA foreign_keys = ON")
   conn.execute("PRAGMA synchronous = NORMAL")
   conn.execute("PRAGMA busy_timeout = 5000")
   conn.autocommit = False
   ```

   - `journal_mode = WAL`: writers don't block readers; multiple
     scufris-server processes against the same DB are safe.
   - `foreign_keys = ON`: FK constraints actually enforced at the
     row level (not the default for sqlite!).
   - `synchronous = NORMAL`: durable through process crashes; not
     durable through OS-level power loss. Acceptable for this
     workload.
   - `busy_timeout = 5000`: wait up to 5s for a contended write
     lock before raising.
   - `autocommit = False` (Python 3.12+ explicit): handlers control
     their own transactions via `with conn:` blocks.

### Transaction policy

- `with conn:` is the unit of atomicity. Commit on exit; rollback
  on exception.
- Handlers can use multiple `with conn:` blocks per request — e.g.
  the chat handler's `sessions.create_channel_link` and
  `_touch_session` are separate atomic units.
- `get_db_conn` does *not* wrap the whole request in a transaction.
  That would hold the writer lock across the slow `send_message`
  call to opencode.
- A handler that crashes between commits leaves the earlier
  successful commits in place. That's intentional — partial state
  is sometimes the right answer (e.g. session created in opencode,
  link persisted, then the message-send call fails — the link is
  still valid for the next attempt).

## Migrations

Mechanism: `store.apply_migrations(conn)` (`store.py:91`).

```
1. Ensure _schema_migrations exists.
2. Read applied filenames from _schema_migrations.
3. List migrations/*.sql in lex order, filter to unapplied.
4. For each pending file:
     BEGIN;
     <run the entire file via executescript>
     INSERT INTO _schema_migrations (filename, applied_at) VALUES (?, strftime('%s','now'));
     COMMIT;
5. Return the list of newly-applied filenames.
```

Properties:

- **Idempotent.** Re-applying an already-applied migration is a
  no-op (it's filtered out before the loop).
- **Atomic.** Each migration's DDL + tracking-row insert is one
  transaction. A crash leaves the schema unchanged.
- **Lex-ordered.** Filenames sort lexicographically. Use
  zero-padded numeric prefixes (`002_`, `010_`, `100_`) to keep
  ordering predictable.
- **Forward-only.** No rollback step. If a migration is wrong, the
  fix is *another* migration that undoes / corrects.

Failure mode if a migration fails:

- The transaction rolls back. `_schema_migrations` doesn't record it.
- `apply_migrations` raises. `app.lifespan` doesn't catch it →
  uvicorn shuts down. The server refuses to boot until you fix the
  migration or revert it.

### Adding a new migration

1. Create `scufris_server/migrations/002_<short_description>.sql`.
2. Write idempotent-friendly DDL where possible (`CREATE TABLE
   IF NOT EXISTS`, `ADD COLUMN` rather than `RENAME COLUMN`).
3. Don't reference any data that might not exist on a fresh DB.
4. Don't edit `001_initial.sql` once it's been applied to a live DB
   — the migration runner has already recorded it as "done" and
   won't re-run it. Schema evolution is `002_*.sql` etc.

### Production migration path

- Today: scufris-server applies migrations at lifespan startup. A
  fresh boot of a new version performs the upgrade automatically.
- Multi-instance deploys (none today, possibly never): the WAL
  + busy-timeout combination means two boots running migrations
  against the same DB will serialise on the writer lock; the
  second one sees the migrations already applied and exits the
  loop with `newly_applied = []`. So it's "safe" but ugly.

## Querying the DB during development

There's no `sqlite3` CLI in the nix devshell. Use Python:

```bash
uv run --active python - <<'PY'
import sqlite3
db = sqlite3.connect("/home/alex/.local/state/scufris/scufris.sqlite")
db.row_factory = sqlite3.Row
for r in db.execute("SELECT * FROM users"):
    print(dict(r))
PY
```

Or for a quick interactive session:

```bash
uv run --active python -m sqlite3 /home/alex/.local/state/scufris/scufris.sqlite
```

Useful queries for debugging:

```sql
-- Who's bound to which surface?
SELECT u.username, sb.surface, sb.surface_id
FROM surface_bindings sb JOIN users u ON u.id = sb.user_id
ORDER BY u.username, sb.surface;

-- Which channels exist for which user?
SELECT u.username, c.surface, c.surface_id, c.agent, sl.oc_session_id, sl.last_used_at
FROM channels c
  JOIN users u ON u.id = c.user_id
  LEFT JOIN session_links sl ON sl.channel_id = c.id
ORDER BY sl.last_used_at DESC;

-- Migrations applied?
SELECT filename, datetime(applied_at, 'unixepoch') FROM _schema_migrations;
```

## Cross-references

- The migration runner: `scufris_server/store.py:91`.
- The connection helper: `scufris_server/store.py:74`.
- The schema source: `scufris_server/migrations/001_initial.sql`.
- Identity behaviour around `users` + `surface_bindings`:
  [`003_identity_and_users.md`](003_identity_and_users.md).
- Chat behaviour around `channels` + `session_links`: `routes/chat.py`.

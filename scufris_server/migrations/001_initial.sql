-- 001_initial.sql
-- Source: tasks/20260613-091036/TASK.md §7 (architecture design doc).
-- Copy is verbatim apart from the leading `CREATE TABLE` keyword on each
-- block — the design renders schemas without it for readability. No fields
-- added, removed, or renamed. To evolve the schema, add 002_*.sql etc.;
-- never edit this file in place once it has been applied to a live DB.

-- Identity (config-driven; rows materialised on first resolve).
CREATE TABLE users (
  id          INTEGER PRIMARY KEY,
  username    TEXT UNIQUE NOT NULL,
  created_at  INTEGER NOT NULL          -- unix seconds
);

CREATE TABLE surface_bindings (
  user_id     INTEGER NOT NULL REFERENCES users(id),
  surface     TEXT NOT NULL,            -- "cli" | "telegram" | "web"
  surface_id  TEXT NOT NULL,            -- "alex", "8231376426", ...
  PRIMARY KEY (surface, surface_id)
);

-- (user, channel) → opencode session.
-- channel = the conversational context: a CLI process, a Telegram
-- chat, eventually a web tab. Sessions are NOT shared across channels.
CREATE TABLE channels (
  id          INTEGER PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id),
  surface     TEXT NOT NULL,
  surface_id  TEXT NOT NULL,            -- chat_id, terminal session id
  agent       TEXT NOT NULL,            -- which scufris agent persona
  UNIQUE (user_id, surface, surface_id, agent)
);

CREATE TABLE session_links (
  channel_id     INTEGER PRIMARY KEY REFERENCES channels(id),
  oc_session_id  TEXT NOT NULL,         -- ses_xxx from opencode
  created_at     INTEGER NOT NULL,
  last_used_at   INTEGER NOT NULL
);

-- Per-user durable facts (long-term memory). Content is plain text;
-- structure (key/value vs free-form) deferred to #19. Concurrent
-- `remember` calls from multiple opencode worker contexts are safe
-- via SQLite WAL + `ON CONFLICT (user_id, key) DO UPDATE` (last
-- writer wins on the same key; independent keys never collide).
CREATE TABLE facts (
  id           INTEGER PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id),
  key          TEXT NOT NULL,
  value        TEXT NOT NULL,
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL,
  UNIQUE (user_id, key)
);

-- Audit log of opencode events we've seen. Cap by row count or age;
-- not the source of truth for conversation content.
CREATE TABLE event_log (
  id            INTEGER PRIMARY KEY,
  ts            INTEGER NOT NULL,       -- unix millis
  oc_session_id TEXT,
  event_type    TEXT NOT NULL,          -- e.g. "message.part.delta"
  payload_json  TEXT NOT NULL
);

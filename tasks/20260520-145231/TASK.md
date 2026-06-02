# User identity, XDG config, and CLI/Telegram session unification

- STATUS: OPEN
- PRIORITY: 80
- TAGS: identity,config,ux,backlog

## The Problem

Right now there are two parallel `user_id` systems that don't know about each other:

- CLI hardcodes `CLI_USER_ID = 1` (or similar constant)
- Telegram uses `update.effective_user.id` — a Telegram-assigned integer like `123456789`

These are different numbers, so the server sees them as two different people. History, facts, scheduled briefings, everything — split across two phantom identities that are physically the same human sitting at the same desk.

## Named Identity Layer

A thin identity registry on the server — a SQLite table, not a full auth system:

```sql
users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, created_at)
surface_bindings(user_id, surface TEXT, surface_id TEXT, UNIQUE(surface, surface_id))
-- surface: "cli", "telegram", "web" (future)
-- surface_id: "alex" for CLI, "123456789" for Telegram
```

Concept: a named user (e.g. `alex`) can be reached from multiple surfaces. Each surface has its own `surface_id` that maps to the same `user_id` internally.

### CLI side
- `SCUFRIS_USER=alex` env var (falls back to `$USER` / `getpass.getuser()`)
- On first connection, the server either finds `alex` in the registry or creates it
- The returned `user_id` (the internal integer) is what flows through to history, facts, scheduled slots — same as always

### Telegram side
- On `/start`, the bot asks: "What's your username? Type it to link this Telegram account to your scufris profile."
- The server binds `surface_id=123456789 → username=alex → user_id=42`
- Subsequent messages from that Telegram chat use `user_id=42` automatically — same history as the CLI

### Result
- Ask something in the CLI, continue in Telegram — same conversation window, same facts, same scratchpad
- Scheduled briefings fire per `user_id`, not per surface — the server delivers to whichever surface is active, or all of them

## XDG User Configuration

A config file at `$XDG_CONFIG_HOME/scufris/config.toml` (defaulting to `~/.config/scufris/config.toml`) that the server and CLI both read on startup. Structured by user identity so it's ready for multi-user without being painful for single-user:

```toml
[user]
username = "alex"
timezone = "Europe/Bucharest"

[user.schedule]
enabled = true

[[user.schedule.slots]]
name = "morning"
time = "07:30"
days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
briefing = "morning"
surfaces = ["telegram"]   # where to deliver — "telegram", "cli", or both

[[user.schedule.slots]]
name = "lunch"
time = "12:30"
days = ["mon", "tue", "wed", "thu", "fri"]
briefing = "lunch"
surfaces = ["telegram"]

[[user.schedule.slots]]
name = "gym"
time = "18:00"
days = ["mon", "wed", "fri"]
briefing = "gym"
surfaces = ["telegram"]

[user.rag]
sources = [
  { name = "journal", path = "~/journal", type = "markdown", watch = true },
  { name = "notes", path = "~/notes", type = "markdown" },
]

[user.journal]
den_path = "~/journal"

[user.notifications]
desktop = true   # notify-send / libnotify when CLI surface is active
telegram = true
```

### Key design decisions
- `surfaces` per slot — morning briefing goes to Telegram only (phone), not the terminal you might not have open
- `timezone` lives here, not hardcoded — used by the scheduler, the journal agent, and the reminder dispatcher
- Config is read by the server at startup and hot-reloaded on `SIGHUP` (no restart needed to change schedule)
- The server never writes to this file — it's the user's territory. Scufris reads it, nothing else.

## What "Per-User" Actually Means in Practice

Even with a single user, the architecture should route everything through `user_id`:

| Thing                  | Per-user? | How                                                        |
| ---------------------- | --------- | ---------------------------------------------------------- |
| Conversation history   | yes       | already keyed by `(user_id, agent)`                        |
| Scheduled briefings    | yes       | scheduler reads `config.toml` per user, fires to surfaces  |
| RAG sources            | yes       | `rag_sources.toml` under the same XDG dir                  |
| Facts / scratchpad     | yes       | already keyed by `user_id`                                 |
| Notifications          | yes       | `surfaces` list per slot in config                         |
| Bearer token           | yes       | `SCUFRIS_TOKEN` in the env file, checked server-side       |

Adding a second user later is: add a row to `users`, create a second `config.toml` (or a `[users.bob]` section), bind their Telegram ID. No architectural change.

## CLI↔Telegram Sync UX

Small touches that make the cross-surface experience feel seamless rather than accidental:

- `/stats` shows active surfaces: `surfaces: cli (last seen 2m ago), telegram (last seen 1h ago)`
- The thinking trace in CLI shows `[telegram]` tag on messages that originated there (if history is shared — this might be too noisy, make it opt-in)
- `/clear` clears history for the user across all surfaces, not just the one you typed it in
- Server returns a `surface` field in the stats response so the CLI can say "your Telegram is linked as @yourhandle"

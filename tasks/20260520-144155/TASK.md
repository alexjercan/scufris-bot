# Create tasks for future springs

- STATUS: CLOSED
- PRIORITY: 100
- TAGS: tasks,planning

Split into 8 backlog tasks (priority 0, tagged `backlog`):

- `20260520-145242` — RAG document store: embeddings, sync, and Q&A
- `20260520-145227` — Morning briefing and scheduled agent prompts via timetable
- `20260520-145228` — Multi-modal image input: CLI path and Telegram attachment
- `20260520-145243` — Proactive follow-up suggestions in agent prompts
- `20260520-145229` — Persistent conversation store: SQLite backend and export
- `20260520-145230` — Tool usage histogram in /stats
- `20260520-145244` — Persistent user facts and per-turn agent scratchpad
- `20260520-145231` — User identity, XDG config, and CLI/Telegram session unification

---


1. RAG / Local Document Store
RAG document store: embeddings, sync, and Q&A [rag, knowledge, tools]
A local vector database (ChromaDB or sqlite-vec — both embeddable, no server needed) that the knowledge agent queries before touching the web. Two layers:
Indexing / sync side:

A rag_sync CLI command (or a tool the agent can call) that takes a source spec and chunks + embeds documents into the store. Sources could be:

A local directory (your journal's daily .md files, a notes vault, a book folder)
A git repo path (re-syncs on pull)
A remote URL list or an Atom/RSS feed of documents
A glob pattern like ~/journal/2026-*.md


Chunking strategy: paragraph-aware for Markdown (respect headings as chunk boundaries), sliding window with overlap for PDFs
Embeddings: nomic-embed-text via Ollama — already local, no API key, reasonable quality
Metadata stored per chunk: source_path, source_type, chunk_index, modified_at, content hash for dedup/incremental sync
Sync is incremental: hash the file, skip if unchanged. A watcher mode (rag_sync --watch ~/journal) using watchdog for live re-indexing as you write

Query side:

rag_search(query, top_k, source_filter?) tool added to the knowledge agent's toolset
Knowledge agent prompt updated: "before calling web_search for personal/historical questions, try rag_search first"
Results include the source path and a snippet so the agent can cite "from your journal entry on May 3rd…"
Optional reranking pass (cross-encoder or just LLM-based) if top_k results are noisy

Journal integration specifically:

Auto-sync hook: after today --create writes a new entry, trigger an incremental sync of ~/journal/ so today's entry is immediately queryable
This lets you ask "what did I eat last Tuesday?" or "what was my weight trend in April?" and get answers from your actual journal rather than hallucinations

Source registry:

A small rag_sources.toml (or stored in the DB itself) listing named sources with their path/URL, sync schedule, and chunking config
rag_sources_tool lets the agent list what's indexed and when it was last synced


2. Morning Briefing + Scheduled Agent Prompts
Scheduled activity briefings via cron + timetable [journal, automation, ux]
Two pieces: a timetable the agent knows about, and a dispatcher that fires prompts at the right times.
Timetable definition:

A structured config (TOML or a dedicated journal section) defining named activity slots: wake_up, breakfast, work, lunch, gym, hobby, dinner, sleep
Each slot has: time (or cron expression for flexible days), days of week, and a briefing type
Example:

toml  [[slots]]
  name = "morning"
  time = "07:30"
  days = ["mon","tue","wed","thu","fri"]
  briefing = "morning"

  [[slots]]
  name = "gym"
  time = "18:00"
  days = ["mon","wed","fri"]
  briefing = "gym"
Dispatcher:

scufris-server runs a background asyncio task (like the reminder dispatcher) that checks the timetable and fires agent prompts at slot times
Each briefing type maps to a system prompt injected into a fresh turn: the agent is told "it's time for X, generate a briefing"
Delivery via the active surface: Telegram message, or a CLI desktop notification (notify-send) if the CLI is not open

Briefing types (examples):

morning — today's tasks from journal, habits to complete, weather, any due reminders, one-line motivational note
gym — today's workout if defined in journal, last session logged, current weight trend
lunch — remaining macros for the day, quick suggestion for what to eat to stay on target
evening — incomplete tasks to roll over, habits not yet ticked, summary prompt ("ready to log dinner?")
sleep — brief EOD summary, tomorrow's first task, reminder to log weight

Cron integration:

scufris-cron script (or systemd.timers entry in the NixOS/HM module) that calls POST /v1/cron/slot {slot: "morning"} on the server
Alternatively: server reads the timetable and self-schedules using asyncio — no external cron needed, but cron is more reliable across suspends/wakeups


3. Image Input (CLI path + Telegram attachment)
Multi-modal image input: path in CLI, attachment in Telegram [multimodal, cli, telegram]
CLI side:

/image <path> slash command, or natural syntax: the user types a message containing a file path ending in .png/.jpg/.webp and the CLI detects it
Image is base64-encoded and attached to the next API call as a vision message
Alternatively: watch for clipboard image (if xclip/wl-paste has image data) when the user types /paste-image

Telegram side:

Already supported by Telegram's Bot API — photos and documents land in message.photo / message.document
main.py handler extended: if the message contains an image, download it to a temp file and attach alongside the text caption

Processing:

Images are passed to the main agent (or routed to knowledge agent) with the vision-capable model
If the current SCUFRIS_MODEL isn't vision-capable, fall back gracefully: "I can't process images with the current model; switch to a vision model"
A describe_image_tool wraps the vision call so sub-agents can request image descriptions too — e.g. the journal agent could describe a food photo and auto-log macros

Practical uses the user might want:

"What's in this photo?" (describe)
"Extract the text from this screenshot" (OCR-like)
"This is my lunch — log the macros" (journal integration)
"Here's an error screenshot from my terminal — what's wrong?" (coding agent)


4. Proactive Follow-up Suggestions
Context-aware follow-up suggestions in agent prompts [ux, prompts]
Primarily a prompt engineering task, but with a small structural hook.
Mechanism:

Each sub-agent prompt gets a ## Proactive Suggestions section listing situations where it should append a short follow-up offer at the end of its response
The main agent prompt instructs Scufris to surface these naturally, not robotically

Examples per agent:
Journal agent:

After logging macros → "You're 340 cal under target. Want me to suggest something to fill the gap?"
After toggling a habit → "3 of 5 habits done today. Want to see which are still open?"
After adding a task → "You have 6 tasks for today. Want me to prioritize them?"

Knowledge agent:

After a weather lookup → "Should I add a 'bring umbrella' reminder for tomorrow morning?"
After a factual answer → "Want me to save this to your notes?"

Coding agent:

After explaining a bug → "Want me to open this file in OpenCode and apply the fix?"

Implementation:

No new code needed initially — just prompt additions
Later: a structured suggestion field in the agent's response JSON that the CLI/Telegram renders as a tappable/clickable affordance (e.g., [y] Yes, suggest meals prompt after a macro log)


5. Conversation Persistence + Export
Persistent conversation store: SQLite backend + export [memory, persistence, cli]
The shift:

ChatHistoryManager currently lives entirely in-process (dict of lists). Replace the backing store with SQLite — same API surface, persistent across restarts
Schema:

sql  messages(id, user_id, agent, role, content, tool_calls_json, ts)
  summaries(user_id, agent, summary, updated_at)
  facts(user_id, agent, key, value, source, ts)

On startup, ChatHistoryManager loads the window from SQLite; on write, it appends a row. Trim still happens in-memory for speed but is also reflected in the DB (soft-delete or archive old rows)
Migration: existing in-memory state is ephemeral so no migration needed — first restart starts fresh from the DB

Export:

GET /v1/export?user_id=N&format=md|json endpoint
Markdown export: one ## heading per turn, assistant responses rendered as-is, tool calls shown as > called weather("Ploiesti")
JSON export: raw message list with metadata, importable back in
/export CLI slash command saves to ~/.scufris/exports/<timestamp>.md

RAG reuse:

The messages table is also a natural corpus for the RAG store — past conversations are documents worth indexing
A sync source type = "conversations" in rag_sources.toml would chunk and embed old turns, letting you ask "what did we figure out last week about the OpenCode issue?"


6. Improved /stats with Tool Usage
Tool usage histogram in /stats [observability, cli]
New data collected:

ChatHistoryManager (or a thin wrapper around the callback handler) tracks per-(user_id, tool_name) call counts, already partially done via _invocations
Extend to track all tools, not just sub-agents — web_search, weather, calculator, macros_entry, etc.

/stats output addition:
Tool usage (this session):
  web_search        ████████  8
  weather           ███       3
  macros_entry      ██        2
  tasks_entry       █         1
  calculator        █         1
ASCII bar chart normalized to the max call count — fits in a terminal, works in Telegram monospace block.
Implementation:

ToolCallbackHandler.on_tool_end already fires for every tool — increment a Counter keyed by tool name there
get_user_telemetry extended to include the tool counter
format_stats_lines gets a second section below the per-agent table


7. Persistent User Facts + Agent Scratchpad
User facts and scratchpad in SQLite (replacing in-memory dicts) [memory, persistence]
The distinction:

User facts (_facts): durable attributes about the person — location, dietary preferences, ongoing projects, preferences. Should survive restarts. Should be queryable.
Agent scratchpad: ephemeral working notes within a single conversation — "I just checked the weather, no need to call again". Can stay in-memory.

Facts persistence:

The facts table from §5 above is exactly this. add_facts writes to SQLite; get_facts reads from it. The in-memory dict becomes a write-through cache.
/stats shows fact count per agent slice
New CLI command: /facts [agent] — lists all stored facts for the user, optionally filtered by agent. Lets you inspect what Scufris "knows" about you.
/forget <key> becomes a durable delete from the DB

Scratchpad (new concept):

A per-turn, per-agent ephemeral key-value store, initialized at the start of each sub_agent_tool call and discarded after
Useful for agents that need to carry state within a multi-step tool chain but don't want to pollute _facts with transient data
Example: journal agent sets scratchpad["today_entry_created"] = True early in a turn so it doesn't call today_create_tool twice
Implementation: just a dict passed into the sub-agent invocation via the composed user message or a ContextVar — nothing persisted

Combined picture:
Scufris memory layers (top = most ephemeral):
  scratchpad    — per-turn, per-agent, in-memory only
  window        — last N messages, SQLite-backed
  summary       — compressed older turns, SQLite
  facts         — durable user attributes, SQLite, cross-restart
  RAG store     — documents + past conversations, vector DB

8. User Identity, Configuration, and Cross-Surface Continuity

Per-user config (XDG), named identity, and CLI↔Telegram session unification [identity, config, ux]
The Problem

Right now there are two parallel user_id systems that don't know about each other:

    CLI hardcodes CLI_USER_ID = 1 (or similar constant)
    Telegram uses update.effective_user.id — a Telegram-assigned integer like 123456789

These are different numbers, so the server sees them as two different people. History, facts, scheduled briefings, everything — split across two phantom identities that are physically the same human sitting at the same desk.
Named Identity Layer

Introduce a thin identity registry on the server — a SQLite table, not a full auth system:
sql

users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, created_at)
surface_bindings(user_id, surface TEXT, surface_id TEXT, UNIQUE(surface, surface_id))
-- surface: "cli", "telegram", "web" (future)
-- surface_id: "alex" for CLI, "123456789" for Telegram

The concept: a named user (e.g. alex) can be reached from multiple surfaces. Each surface has its own surface_id that maps to the same user_id internally.

CLI side:

    SCUFRIS_USER=alex env var (falls back to $USER / getpass.getuser())
    On first connection, the server either finds alex in the registry or creates it
    The returned user_id (the internal integer) is what flows through to history, facts, scheduled slots — same as always

Telegram side:

    On /start, the bot asks: "What's your username? Type it to link this Telegram account to your scufris profile."
    The server binds surface_id=123456789 → username=alex → user_id=42
    Subsequent messages from that Telegram chat use user_id=42 automatically — same history as the CLI

The result:

    Ask something in the CLI, continue in Telegram — same conversation window, same facts, same scratchpad
    Scheduled briefings fire per user_id, not per surface — the server delivers to whichever surface is active, or all of them

XDG User Configuration

A config file at $XDG_CONFIG_HOME/scufris/config.toml (defaulting to ~/.config/scufris/config.toml) that the server and CLI both read on startup. Structured by user identity so it's ready for multi-user without being painful for single-user:
toml

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

Key design decisions:

    surfaces per slot — morning briefing goes to Telegram only (phone), not the terminal you might not have open
    timezone lives here, not hardcoded — used by the scheduler, the journal agent, and the reminder dispatcher
    Config is read by the server at startup and hot-reloaded on SIGHUP (no restart needed to change schedule)
    The server never writes to this file — it's the user's territory. Scufris reads it, nothing else.

What "Per-User" Actually Means in Practice

Even with a single user (you), the architecture should route everything through user_id:
Thing	Per-user?	How
Conversation history	✅	already keyed by (user_id, agent)
Scheduled briefings	✅	scheduler reads config.toml per user, fires to bound surfaces
RAG sources	✅	rag_sources.toml under the same XDG dir
Facts / scratchpad	✅	already keyed by user_id
Notifications	✅	surfaces list per slot in config
Bearer token	✅	SCUFRIS_TOKEN in the env file, checked server-side

Adding a second user later is: add a row to users, create a second config.toml (or a [users.bob] section), bind their Telegram ID. No architectural change.
CLI↔Telegram Sync UX

A few small touches that make the cross-surface experience feel seamless rather than accidental:

    /stats shows active surfaces: surfaces: cli (last seen 2m ago), telegram (last seen 1h ago)
    The thinking trace in CLI shows [telegram] tag on messages that originated there (if history is shared — this might be too noisy, make it opt-in)
    /clear clears history for the user across all surfaces, not just the one you typed it in
    Server returns a surface field in the stats response so the CLI can say "your Telegram is linked as @yourhandle"


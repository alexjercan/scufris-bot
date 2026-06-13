# Design doc: scufris-v2 architecture around opencode

- STATUS: CLOSED
- PRIORITY: 90
- TAGS: design,architecture

Replaces v1's multi-agent design doc. Define how Scufris maps onto
opencode sessions/agents, where the HTTP layer sits, and what we own
vs. what opencode owns. Should land before the tools/server/client
implementation tasks pick up; consumes the output of the opencode
capabilities spike.

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).

---

## 1. Goals

- Replace v1's hand-rolled LangChain agent loop with `opencode` as the
  agent runtime, dropping the supporting infrastructure (tool
  callbacks, sub-agent dispatch, per-agent prompt routing,
  context-window management) that opencode already provides.
- Keep v1's user-facing surface area roughly intact: a HTTP server,
  a CLI client, a Telegram bot, single-host deployment via Nix +
  systemd, named-user identity, per-user config.
- Make the integration boundary with opencode as small as possible:
  one HTTP/SSE client and one in-tree opencode plugin. No fork of
  opencode, no patches.
- Preserve v1's deployment ergonomics: one `nix run` brings up the
  daemon; the CLI is a thin client.

## 2. Non-goals

- Multi-tenant identity / per-user auth beyond v1's named-user
  registry. opencode's HTTP server has a single shared password; that
  fits localhost-only single-user deployments and is not changed here.
- Web UI. Future work, called out in #15/#16's neighbourhood.
- Sandboxing of tool execution beyond opencode's permission model and
  systemd hardening.
- Replacing `opencode serve` itself. We treat it as a stable HTTP
  dependency, the same way we treat ollama.
- Persisting opencode session content ourselves. opencode owns that;
  we only persist *pointers* to its sessions.

## 3. Inputs to this doc

- The opencode runtime spike: `tasks/20260613-091035/TASK.md`. Its
  capability matrix and Decisions A–G are the foundation here; this
  doc formalises and extends them.
- The v1 deployment design spike: `tasks/20260510-192350/TASK.md`. The
  HTTP/SSE shape, framework choice (FastAPI), bind/auth model, and
  graceful-shutdown story carry over largely unchanged.
- The v1 identity + XDG config task: `tasks/20260520-145231/TASK.md`.
  Closed and shipped — `~/.config/scufris/config.toml` plus
  `[user.identity]` mapping. Carry over verbatim into v2.

## 4. System overview

```
                  ┌────────────────────────────────────────────────┐
                  │                  user host                     │
                  │                                                │
  Telegram ───────┼─► scufris-tg ─┐         ┌─► opencode serve ───┼─► ollama / cloud LLMs
                  │  (process)    │         │  (process)          │
                  │               ▼         ▼                     │
  terminal  ──────┼─► scufris-cli ─► scufris-server ◄─► sqlite    │
                  │  (process)        (process)         (file)    │
                  │                       ▲                       │
                  │                       │                       │
                  │   .opencode/plugin/scufris.ts  ◄──── plugin   │
                  │   (in opencode's process)       hook IPC      │
                  │                                                │
                  └────────────────────────────────────────────────┘
```

**Three long-running processes**, all on the same host by default:

1. `scufris-server` — Python/FastAPI daemon. Owns identity, channel
   ↔ session mapping, per-user facts, config, audit log. Talks to
   opencode over loopback HTTP. Exposes the public `/v1/*` API.
2. `opencode serve` — third-party. Owns sessions, agent loop, tools,
   compaction, permissions. Bound to `127.0.0.1` only.
3. `scufris-tg` and `scufris-cli` — clients of `scufris-server`. The
   CLI is interactive; the bot is long-lived.

**One in-process plugin** runs *inside* opencode:
`.opencode/plugin/scufris.ts` registers tools, hooks compaction for
fact injection, and forwards permission-flow events. It never talks
to scufris-server directly — it communicates via opencode's plugin
ABI (events, tools, hooks).

## 5. Components

### 5.1 scufris-server (Python, FastAPI)

Single async process. Modules:

- `scufris_server.app` — FastAPI app, lifespan owner.
- `scufris_server.opencode_client` — `httpx.AsyncClient` against
  `http://127.0.0.1:4096`. One client instance, shared. Endpoints
  used: `/session*`, `/api/session*`, `/event`, `/agent`, `/provider`,
  `/config`, `/global/health`.
- `scufris_server.events` — single SSE consumer task tailing
  `GET /event`, dispatching to per-session in-memory queues; reconnects
  on disconnect.
- `scufris_server.identity` — wraps `~/.config/scufris/config.toml`
  resolution (carried from v1).
- `scufris_server.store` — SQLite, one file at
  `$XDG_STATE_HOME/scufris/scufris.sqlite`. Tables in §7.
- `scufris_server.routes` — `/v1/chat`, `/v1/chat/stream`,
  `/v1/sessions`, `/v1/identity/resolve`, `/v1/stats`, `/v1/clear`,
  `/v1/permissions/:id/reply`, `/v1/healthz`, `/v1/version`.
- `scufris_server.facts` — read API for the per-user fact store; the
  *write* path goes through the opencode plugin's `remember`/`forget`
  tools, not the HTTP API.

The process never spawns `opencode serve` itself. Lifetimes are
managed by systemd (Requires/After). On startup, scufris-server
**polls** opencode `/global/health` until ready or times out.

### 5.2 opencode runtime

`opencode serve --port 4096 --bind 127.0.0.1` running with the
project's worktree as cwd. Configured via:

- `opencode.json` at the project root (committed): providers,
  agents, default permissions, MCP servers, compaction tuning.
- `.opencode/agent/*.md` (committed): scufris-specific agents
  (e.g. `journal`, `coach`).
- `.opencode/tool/*.ts` and/or `.opencode/plugin/scufris.ts`: tools
  and plugin hooks (see 5.3).
- `OPENCODE_SERVER_PASSWORD` from the systemd EnvironmentFile (so
  even loopback access requires the basic-auth secret that
  scufris-server holds).

Configuration is **not hot-reloaded** — `systemctl restart opencode`
on changes.

### 5.3 .opencode/plugin/scufris.ts (plugin)

Single TS file, in this repo. Acts as the *in-process* bridge between
scufris-server's data and opencode's agent loop:

- Registers fact-management tools: `remember`, `forget`, `list_facts`.
  Implementation calls scufris-server over loopback HTTP (the plugin
  process can reach it; scufris-server holds the user-facts source
  of truth).
- Hooks `experimental.session.compacting`: injects per-user facts
  into the compaction summary so they survive context truncation.
- Hooks `tool.execute.before`: lightweight allow-list check on top
  of opencode's permissions (e.g. forbid `bash` on holiday Mondays).
  Most policy still lives in `opencode.json`.
- Emits structured logs that scufris-server can correlate via the
  session ID (no direct push — scufris-server already sees these via
  SSE).

The plugin is the *only* place we extend opencode itself. Anything
that can be expressed as a custom tool in `.opencode/tool/*.ts`
without needing hooks goes there instead, to keep the plugin small.

### 5.4 scufris-cli (Python, prompt_toolkit)

REPL talking to scufris-server. Connects to `/v1/chat/stream`
(SSE), renders thinking events using a renderer ported from v1.
Slash commands: `/clear`, `/stats`, `/sessions`, `/fork`,
`/revert`, `/help`. Resolves identity via
`POST /v1/identity/resolve {surface: "cli", surface_id: $USER}` once
on startup, caches the returned user_id.

### 5.5 scufris-tg (Python, python-telegram-bot)

Telegram adapter, also a thin client of scufris-server. Adds:

- Inline-keyboard handling for permission prompts (#30).
- Edit-in-place message rendering as `message.part.delta` events
  arrive (rate-limited to satisfy Telegram's API).
- Surface ID resolution: `update.effective_user.id` → user_id via
  `POST /v1/identity/resolve {surface: "telegram", surface_id: ...}`.

## 6. Process & deployment topology

**Default deployment** (single user, one host, NixOS):

- One systemd unit each: `opencode.service`, `scufris-server.service`,
  `scufris-tg.service`. CLI is run interactively, no unit.
- `opencode.service` is the upstream package. We do **not** vendor it.
- `scufris-server.service` `Requires=` and `After=` opencode. On
  opencode restart, scufris-server's SSE consumer reconnects (the
  consumer's job is to be tolerant of `/event` flapping).
- The Telegram unit `Requires=` scufris-server.

**Multi-project** is explicitly v0-out-of-scope. The whole stack
assumes one opencode = one project = one worktree. Adding more
later means: another `opencode-<projectName>.service` + a
project-router in scufris-server. Not designed in detail here.

**Bind addresses.** Both scufris-server and opencode bind to
`127.0.0.1` by default. Telegram is the only public surface, and
it's outbound-only (long-poll or webhook with HTTPS terminated by
the platform). No inbound TLS in v1; if remote-from-laptop is ever
needed, terminate at a reverse proxy and use a bearer token (carried
over from v1 design — see #14).

## 7. Data model

Database: SQLite at `$XDG_STATE_HOME/scufris/scufris.sqlite`. WAL.

```sql
-- Identity (config-driven; rows materialised on first resolve).
users (
  id          INTEGER PRIMARY KEY,
  username    TEXT UNIQUE NOT NULL,
  created_at  INTEGER NOT NULL          -- unix seconds
);

surface_bindings (
  user_id     INTEGER NOT NULL REFERENCES users(id),
  surface     TEXT NOT NULL,            -- "cli" | "telegram" | "web"
  surface_id  TEXT NOT NULL,            -- "alex", "8231376426", ...
  PRIMARY KEY (surface, surface_id)
);

-- (user, channel) → opencode session.
-- channel = the conversational context: a CLI process, a Telegram
-- chat, eventually a web tab. Sessions are NOT shared across channels.
channels (
  id          INTEGER PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id),
  surface     TEXT NOT NULL,
  surface_id  TEXT NOT NULL,            -- chat_id, terminal session id
  agent       TEXT NOT NULL,            -- which scufris agent persona
  UNIQUE (user_id, surface, surface_id, agent)
);

session_links (
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
facts (
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
event_log (
  id            INTEGER PRIMARY KEY,
  ts            INTEGER NOT NULL,       -- unix millis
  oc_session_id TEXT,
  event_type    TEXT NOT NULL,          -- e.g. "message.part.delta"
  payload_json  TEXT NOT NULL
);
```

**What lives where:**

| Data                     | Owner               | Why                                           |
|--------------------------|---------------------|-----------------------------------------------|
| Conversation messages    | opencode            | sessions persist there; we never duplicate    |
| Tool inputs/outputs      | opencode            | part of the session record                    |
| Token / cost accounting  | opencode            | per-message; we copy summaries on demand      |
| Compaction summaries     | opencode            | written by its compaction agent               |
| User identity            | scufris (sqlite)    | config-driven, materialised on first resolve  |
| Channel ↔ session map    | scufris (sqlite)    | opencode has no concept of "channel"          |
| Facts (long-term memory) | scufris (sqlite)    | shared across all sessions for that user      |
| User config              | filesystem (TOML)   | `~/.config/scufris/config.toml` (read-only)   |
| opencode config          | filesystem (JSON+md)| `opencode.json` + `.opencode/agent/*.md`      |

## 8. Public HTTP API (scufris-server)

Largely the v1 surface, adapted to opencode. All paths under `/v1/`.
JSON unless specified. Authentication: optional bearer token from
`SCUFRIS_TOKEN` env var; if unset, all bound interfaces are loopback
and unauthenticated (v1 model, deferred to #14 for hardening).

| Method | Path                            | Body / Query                                                               | Response                                                                 |
|--------|---------------------------------|----------------------------------------------------------------------------|--------------------------------------------------------------------------|
| POST   | `/v1/identity/resolve`          | `{surface, surface_id}`                                                    | `{user_id, username, surface, surface_id, bound_surfaces}`               |
| POST   | `/v1/chat`                      | `{user_id, channel: {surface, surface_id, agent?}, message, parts?}`       | `{reply: string, oc_session_id, oc_message_id, tokens, cost}`            |
| POST   | `/v1/chat/stream`               | same as `/v1/chat`                                                         | SSE stream of `ThinkingEvent`-shaped events, terminating in `done`       |
| GET    | `/v1/sessions`                  | `?user_id=...`                                                             | `[{channel, oc_session_id, last_used_at, title, tokens}]`                |
| POST   | `/v1/sessions/:channel_id/fork` | `{at_message_id?}`                                                         | `{oc_session_id}` (new session id)                                       |
| POST   | `/v1/sessions/:channel_id/clear`| `{}`                                                                       | `{cleared: true}` — drops the session_link, opencode session unchanged   |
| POST   | `/v1/clear`                     | `{user_id}`                                                                | `{count}` — drops all session_links for that user                        |
| GET    | `/v1/stats`                     | `?user_id=...`                                                             | per-user, per-channel summary (tokens, cost, last_used)                  |
| POST   | `/v1/permissions/:perm_id/reply`| `{decision: "allow"|"once"|"deny"}`                                        | `{ok: true}` — proxied to opencode                                       |
| GET    | `/v1/healthz`                   | —                                                                          | `{ok, opencode: {healthy, version}}`                                     |
| GET    | `/v1/version`                   | —                                                                          | `{version, opencode_version}`                                            |

**SSE event schema for `/v1/chat/stream`** mirrors the v1
`ThinkingEvent` JSON shape — but the *source* events are opencode's
`message.part.*` and `session.*`. Translation happens in
`scufris_server.events`. Terminal event: `{type: "done", message: {...
final assistant message ...}, tokens, cost}`. This keeps v1 clients
binary-compatible at the SSE layer.

## 9. Lifecycles

### 9.1 Synchronous chat (`POST /v1/chat`)

1. Client sends `{user_id, channel, message}`.
2. Server resolves `(user_id, channel) → oc_session_id`. If absent,
   `POST /session {}` to opencode; record the link.
3. Server `POST /session/:id/message` with `{model, agent, system,
   tools, parts: [{type:"text", text: message}]}`. Synchronous —
   blocks until opencode returns the assistant message.
4. Server returns the assistant text + summary metadata. opencode
   keeps everything else.

### 9.2 Streaming chat (`POST /v1/chat/stream`)

1. Same identity / session resolution as 9.1.
2. Server registers a per-message subscription with the
   `scufris_server.events` consumer (filter by oc_session_id +
   message id).
3. Server fires `POST /session/:id/prompt_async`. Returns immediately.
4. Events arrive on the bus; server filters and translates each
   matching event into a `ThinkingEvent`-shaped SSE chunk.
5. On `session.idle`, server fetches the final message via
   `GET /session/:id/message/:msgID` (or it's already accumulated),
   emits `done`, closes the SSE.

### 9.3 Permission flow

1. opencode emits `permission.asked`. scufris-server's event
   consumer routes by oc_session_id → channel → surface.
2. For Telegram: bot sends an inline-keyboard prompt; user taps;
   bot calls `POST /v1/permissions/:perm_id/reply`.
3. For CLI: server emits a `permission.asked` SSE event; client
   prints choices; user types; client posts the reply.
4. Server proxies to `POST /session/:id/permissions/:perm_id`.
5. opencode unblocks and continues the agent loop. Detail in #30.

### 9.4 Fork and revert (new in v2)

opencode supports both natively. Surfaced as:

- `POST /v1/sessions/:channel_id/fork {at_message_id?}` —
  creates a new opencode session forked at the given message **and a
  new channel row pointing at it**. The original channel keeps
  pointing at the parent session; the two are independent threads
  going forward. Caller gets back the new `channel_id` (and the new
  `oc_session_id` for reference). Matches git-branch semantics:
  forking is *additive*, never rebinding.
- Revert is *not* exposed to clients in v1. opencode's
  `POST /session/:id/revert` is reachable directly for an operator
  who needs it, but it rewrites session history in place — destructive
  enough to want a confirmation UX before we surface it through
  scufris-server. The hooks are present in the data model
  (`session_links` row carries the `oc_session_id`) so adding a
  scufris endpoint later is a small change.

### 9.5 Compaction

opencode owns it. Triggered by `compaction.auto` config or by
`POST /api/session/:id/compact`. Our plugin's
`experimental.session.compacting` hook injects:

- The user's facts (read from scufris-server over loopback HTTP).
- Channel metadata (which surface, which agent persona).

Compaction summaries land back in opencode's session record. We do
not store them.

## 10. opencode integration details

### 10.1 Client

- Single `httpx.AsyncClient(base_url="http://127.0.0.1:4096",
  auth=("", $OPENCODE_SERVER_PASSWORD), timeout=...)`.
- Per-call timeouts are generous (LLM-bound). The streaming path
  uses `httpx-sse` or `aiohttp.sse_client` (TBD; pick the one that
  composes with FastAPI's lifespan most cleanly — leaning
  `httpx-sse` for consistency).
- No auto-generated client. The OpenAPI is large but we use a small
  subset; hand-roll typed wrappers in `opencode_client.py`.

### 10.2 Plugin (`.opencode/plugin/scufris.ts`)

- One default-export object with hook methods:
  `tool({ register })`, `event({ on })`,
  `experimental.session.compacting({ ... })`.
- Uses a small inlined HTTP client (`fetch`) to talk to
  scufris-server at `http://127.0.0.1:${SCUFRIS_PORT:-7080}`. Auth:
  shared secret in `SCUFRIS_PLUGIN_TOKEN`.
- Compiled / loaded by opencode at startup. No build step in our
  flake; we ship the .ts file and rely on opencode's own loader.

### 10.3 Custom tools (`.opencode/tool/*.ts`)

- Pure functions, no scufris-server dependency:
  `today.ts`, `daily.ts`, `weather.ts`, `web_search.ts`,
  `calculator.ts`, `datetime.ts` — these match v1's tool list.
- Tools needing user context (e.g. journal tools that need
  `[user.journal].den_path`) read it via opencode's environment or
  via a thin HTTP call to scufris-server's `/v1/user/:id/config`
  (deferred — for v0, hard-code the single-user path).

### 10.4 MCP

Optional. Reserved as the escape hatch for tools we don't want to
write inline (e.g. third-party integrations). Configure in
`opencode.json` `mcp.<name>: {...}`. None planned for v0.

## 11. Identity layer (carried from v1)

Unchanged from v1's shipped design (see
`tasks/20260520-145231/TASK.md`):

- `~/.config/scufris/config.toml` is the source of truth for
  username + per-surface IDs (`[user.identity]`).
- `POST /v1/identity/resolve` materialises a row in `users` /
  `surface_bindings` on first call, returns the `user_id`.
- `SCUFRIS_USER_ID` env var override still wins for the CLI.
- Multi-user support is not in v0; the schema accommodates it.

## 12. Configuration files

| File                                       | Owner    | Purpose                                                |
|--------------------------------------------|----------|--------------------------------------------------------|
| `~/.config/scufris/config.toml`            | user     | identity, schedule, journal path, RAG sources          |
| `$XDG_STATE_HOME/scufris/scufris.sqlite`   | server   | identity registry, session links, facts, audit log     |
| `<project>/opencode.json`                  | repo     | providers, default agent, default perms, MCP servers   |
| `<project>/.opencode/agent/*.md`           | repo     | per-agent prompts/models/permissions                   |
| `<project>/.opencode/tool/*.ts`            | repo     | custom tools                                           |
| `<project>/.opencode/plugin/scufris.ts`    | repo     | hooks, fact-injection, custom MCP wiring               |
| `/run/secrets/scufris.env` (NixOS)         | sops     | `OPENCODE_SERVER_PASSWORD`, `SCUFRIS_TOKEN`, etc.      |

## 13. Observability

- **Token / cost.** Every assistant message from opencode has
  `info.tokens` and `info.cost`. scufris-server aggregates per
  (user, channel, day) into a derived view (#28).
- **Logs.** Structured JSON to stdout; journald collects. Request IDs
  propagated from `/v1/*` into log lines. opencode's own logs go to
  its own journald unit.
- **Tracing.** Out-of-scope for v0. The audit `event_log` table is the
  closest thing we have.
- **Health.** `/v1/healthz` returns scufris status + a probe of
  `opencode /global/health`.

## 14. Security model

- Default bind: `127.0.0.1` everywhere.
- opencode is gated by `OPENCODE_SERVER_PASSWORD` even on loopback
  (HTTP basic; single shared secret). scufris-server holds it.
- scufris-server's `/v1/*` is unauthenticated on loopback; gated by
  `SCUFRIS_TOKEN` bearer when non-loopback bind is configured (#14).
- The Telegram bot uses the platform's own auth; scufris-server trusts
  the bot's `surface_id` claim because they share a host.
- The plugin → server channel uses `SCUFRIS_PLUGIN_TOKEN` (separate
  secret to keep blast radius small if the plugin token leaks).
- NixOS systemd units: `DynamicUser`, `ProtectSystem=strict`,
  `ReadWritePaths` to the state dir only, `RestrictAddressFamilies`
  to AF_UNIX/AF_INET, `MemoryDenyWriteExecute=yes` where viable.

## 15. Architecture decision records

These formalise the spike's Decisions A–G plus the new ones the
spike's findings unlocked.

### ADR-1: One opencode process per project (= per host, for v0).

opencode binds project to cwd-at-startup; multi-project means multiple
processes. v0 ships one. Multi-project routing is a follow-up.

### ADR-2: Python scufris-server, hand-rolled httpx client.

No official Python SDK. The OpenAPI is large but the surface we use
is small. Codegen is overkill and hides the integration; hand-roll
typed wrappers and only generate later if churn warrants it.

### ADR-3: scufris owns identity; opencode is single-secret.

opencode auth is HTTP basic with a single shared password — fine for
gating loopback, useless for multi-user. v0 single-user; v1's
`users` / `surface_bindings` schema preserved verbatim. Any
multi-user feature lives in scufris-server's layer in front of
opencode, never in opencode itself.

### ADR-4: Prefer opencode-native config over scufris-side config.

Agents, tools, MCP, permissions all live under `opencode.json` and
`.opencode/`. scufris config covers only what opencode doesn't know
about: identity, channel policy, secrets routing, plugin enablement.
Avoids two parallel configuration systems for overlapping concerns.

### ADR-5: Facts and per-user prefs injected via opencode plugin.

Per-user durable memory lives in scufris's SQLite. It reaches the
LLM via `experimental.session.compacting` (always present in
context, summarised across compactions) plus, for high-priority
turns, via the per-message `system` override on the chat endpoint.
Avoids us managing the full context window — opencode does that.

### ADR-6: Structured output via opencode's `format.json_schema`.

Drop v1's regex parser. opencode injects a synthetic
`StructuredOutput` tool, validates server-side, surfaces the parsed
object at `info.structured`. Robust and free.

### ADR-7: Render UI from `message.part.delta` + `message.part.updated`.

Sufficient for streaming chat. The finer-grained `session.next.*`
event family is reserved for instrumentation; not wired to the UI.

### ADR-8: scufris-server is *not* responsible for spawning opencode.

systemd is. This decouples lifecycles, leverages NixOS hardening, and
matches how we run ollama. Failure mode: scufris-server reports
`opencode unhealthy` and serves degraded responses.

### ADR-9: Plugin is checked into this repo, not packaged separately.

`.opencode/plugin/scufris.ts` is committed. Avoids the npm publishing
loop and keeps the plugin in lockstep with the server it talks to.
If we ever need to share it, vendor it then.

### ADR-10: One persistent SSE consumer per scufris-server process.

A single long-lived `GET /event` reader. Per-message subscriptions
are in-memory queues that `/v1/chat/stream` handlers register
against. Reconnect-on-drop logic in one place. Avoids N consumers
for N concurrent streams.

### ADR-11: SQLite for v0, no migration plan to Postgres.

Single-host, single-user. WAL mode. Schema migrations via a
hand-rolled `apply_migrations()` step at startup. If we ever go
multi-tenant remote, revisit; not before.

### ADR-12: Plugin token rotation requires restart of both processes.

`SCUFRIS_PLUGIN_TOKEN` lives in opencode's environment and in
scufris-server's. Rotation = restart both. Acceptable for the
single-host deployment model; equivalent to the existing
"restart on `opencode.json` change" story. Document in the secrets
guide (#25) when written. Not worth a hot-reload mechanism.

### ADR-13: Fork creates a new channel; revert is deferred.

Resolves §16.2 and §16.5. `POST /v1/sessions/:channel_id/fork`
allocates a new `channels` row pointing at the new opencode session;
the parent channel is unchanged. Revert (`POST /session/:id/revert`
in opencode) is intentionally not surfaced through scufris-server
until a confirmation UX exists in at least one client; reachable
via the opencode API directly for operators in the meantime.

## 16. Risks & open questions

These map to follow-up tasks; calling them out here so they're not
forgotten when implementation begins. Each is a deliberate deferral,
not a blocker on this design.

### 16.1 SSE reconnect — empirical verification deferred to #11.

The design assumes opencode's `/event` bus is fire-and-forget (events
emitted during a consumer disconnect are lost). The single persistent
consumer + reconcile-by-polling-`/session/:id/message` mitigation in
§5.1 handles either behaviour. Empirical verification (drop the
consumer mid-stream, observe what's missed) is implementation work
and lives in #11's acceptance tests.

### 16.2 Latency budget — measurement deferred to #29.

Two added hops (client → scufris-server → opencode → ollama) vs.
v1's in-process LangChain. Not design-blocked; measurement-blocked.
#29 (perf baseline) needs to specifically measure round-trip
through both hops and compare to a v1 reference if one is available.

### 16.3 Cost / token quotas — deferred until #28 lands.

opencode emits per-message cost; scufris-server can aggregate per
user. Per-user *budgets* (cap, alert, refuse) require both an
aggregation pipeline (#28) and a surface for the user to set/inspect
the budget. Both prerequisites missing in v0; revisit once #28 is
emitting clean per-user cost data, then file a budgets task with
real context.

## 17. Backlog impact (consumes spike #1's revisions)

This doc adopts the spike's task-status revisions verbatim and adds
the additional follow-ups uncovered while writing it:

| #  | Task                                | Status after this doc                                                |
|----|-------------------------------------|----------------------------------------------------------------------|
| #3 | Per-(user, agent) sessions spike    | shrink — answers a single question (channel granularity = §16.2)     |
| #9 | scufris-server v2 (HTTP daemon)     | foundation — implements §5.1, §8                                     |
| #10| Per-user opencode session mgmt      | foundation — implements §7 channel/session_link tables, §9.1         |
| #11| SSE streaming                       | foundation — implements §5.1 events module, §9.2, §16.1              |
| #12| Identity layer                      | unchanged from v1; carries over verbatim                             |
| #18| SQLite session store                | shrunk — only the schema in §7, not session content                  |
| #19| User facts store + tools            | implements §5.3 plugin tools + the `facts` table in §7               |
| #20| Compaction strategy                 | mostly deletable — opencode owns it; the only work is §9.5 hook      |
| #28| Server observability                | implements §13                                                       |
| #29| Perf baseline                       | resolves §16.6                                                       |
| #30| Permissions UX bridge               | implements §9.3                                                      |

New follow-ups suggested by writing this doc (filed before
implementation begins):

- **#31 (`20260613-093911`) Plugin scaffolding & build path.** Where
  the plugin lives, how it's loaded by opencode at start, whether it
  needs a bundler step, and how we test it. Mentioned in §5.3 / §10.2
  but not drilled down. Priority 70.
- **#32 (`20260613-093857`) Plugin ↔ server protocol contract.** Locks
  down the HTTP contract for `remember` / `forget` / `list_facts` /
  compaction-time context injection, and the
  `SCUFRIS_PLUGIN_TOKEN` auth model (§14). Gated `/v1/internal/*`
  endpoints. Priority 60. Depends on #19 and #31.

## 18. Migration / retirement of v1

Out of scope for this doc; tracked as #27 (`20260613-091102`). Brief
position: v1 stays on its own branch (`main` or equivalent), runs
unchanged until v2 reaches feature parity. v2 lives on
`feature/opencode-v2`. Cutover happens by switching the systemd
unit's package; rollback is `systemctl rollback`.

## 19. Acceptance / closing the loop

This doc lands when:

- [x] Spike #1 findings are consumed (capability matrix + Decisions
      A–G turn into ADRs 1–11 above).
- [x] Component model, data model, lifecycles documented end-to-end.
- [x] HTTP API surface enumerated; SSE event translation contract
      noted.
- [x] Deployment topology described (3 systemd units, loopback
      defaults).
- [x] Backlog impact reflected; follow-up tasks #31 / #32 surfaced.
- [x] Reviewed by the user — §16.2 resolved as "fork creates a new
      channel" (ADR-13). §16.1 / §16.6 / §16.7 deferred to existing
      tasks (#11 / #29 / #28). §16.3 / §16.4 / §16.5 resolved inline
      as ADR-12 / footnote on `facts` table / §9.4 update.

Once §19's checkbox is ticked, mark this task CLOSED.

# 001 — Architecture

The 30-second version: scufris-server is a Python/FastAPI daemon that
sits in front of `opencode serve` and exposes a stable HTTP API to
user-facing clients. opencode owns the LLM agent loop, tools, and
sessions. scufris owns identity, channel ↔ session mapping, per-user
state, and the public API surface.

This doc covers:

1. The 3-process topology.
2. What lives in `scufris_server/` and what each module does.
3. The full lifespan — what happens between process start and the
   first request being served.
4. The request flow for `POST /v1/chat`, end-to-end.
5. The dependency-injection pattern that wires it all together.
6. What's deliberately out of scope today.

## Topology

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

Three long-running processes, all on the same host by default:

1. **`scufris-server`** — Python/FastAPI daemon. The subject of these
   docs. Owns identity, channel ↔ session mapping, per-user facts
   (eventually), config, audit log. Talks to opencode over loopback
   HTTP. Exposes `/v1/*` to clients. Source: `scufris_server/`.

2. **`opencode serve`** — third-party. Owns sessions, the agent
   loop, tool execution, compaction, the permission flow. Bound to
   `127.0.0.1:4096` by default. Configured via `opencode.json` and
   `.opencode/`. We treat it as a stable HTTP dependency the same way
   we treat ollama.

3. **Clients** (`scufris-tg`, `scufris-cli`, future web) — talk to
   `scufris-server`, never to opencode directly. Today only
   `scufris-cli` exists in stub form (v2 CLI in #14).

There is also one in-process plugin — `.opencode/plugin/scufris.ts` —
that runs *inside* opencode (not yet implemented; design doc §5.3).
It will register tools, hook compaction for fact injection, and
forward permission events. It does not talk to scufris-server
directly; it uses opencode's plugin ABI.

The "scufris-server ↔ opencode" boundary is deliberately thin: one
HTTP/SSE client (`scufris_server.opencode_client`) and one in-tree
plugin. No fork of opencode, no patches.

## Module map

```
scufris_server/
├── __init__.py            # version constant
├── __main__.py            # `python -m scufris_server` entry point
├── app.py                 # FastAPI factory + lifespan
├── config.py              # Settings (env-driven)
├── dependencies.py        # FastAPI Depends() helpers
├── identity.py            # User resolver + TOML loader (#12)
├── opencode_client.py     # Async httpx wrapper for opencode HTTP API
├── store.py               # SQLite connection helpers + migrations
├── logging.py             # JsonFormatter + RequestIdMiddleware
├── events.py              # ThinkingEvent + per-process EventBus (#11)
├── event_mapping.py       # Pure mapping of opencode events → ThinkingEvent (#11)
├── sse.py                 # SSE wire framing + keepalive helper (#11)
├── internal/              # Reserved for plugin-only HTTP surface
├── migrations/
│   └── 001_initial.sql    # Initial schema
└── routes/
    ├── __init__.py        # ROUTERS list — what gets mounted
    ├── health.py          # /v1/healthz, /v1/version
    ├── identity.py        # /v1/identity/resolve  (#12)
    ├── chat.py            # /v1/chat
    ├── chat_stream.py     # /v1/chat/stream (#11)
    ├── sessions.py        # /v1/sessions, /v1/sessions/{id}/clear, /v1/clear (#10)
    ├── stats.py           # placeholder (#13)
    └── permissions.py     # placeholder (#30)
```

### Roles in one line each

| Module | Job |
|--------|-----|
| `app.py`            | Build the FastAPI instance. Run the startup/shutdown lifespan. Wire middleware + routers. |
| `config.py`         | Read every env var into a single `Settings` pydantic model. Cache it process-wide via `get_settings()`. |
| `dependencies.py`   | The bridge between FastAPI handlers and `app.state`: `get_opencode_client`, `get_db_conn`, `get_user_identity`, `get_identity_override`. |
| `identity.py`       | Pure-Python identity layer. `load_user_identity()` parses TOML; `resolve_user()` does the four-step resolver. No FastAPI dependency. |
| `opencode_client.py`| Typed async wrapper around opencode's HTTP API. Has its own exception hierarchy (`OpencodeNetworkError`, `OpencodeServerError`, etc.). |
| `store.py`          | `connect()` context manager (used by lifespan + tests + `get_db_conn`); `apply_migrations()` (idempotent). Sets WAL + foreign-keys + autocommit=False per connection. |
| `logging.py`        | One JSON object per log line. Per-request `request_id` propagated via `ContextVar`. Pure-ASGI middleware (avoids `BaseHTTPMiddleware` body buffering). |
| `events.py`         | `ThinkingEvent` dataclass (wire shape for the streaming UX) + `EventBus`: a single long-lived `GET /event` consumer per scufris-server process (ADR-10). `subscribe(session_id)` hands out an in-memory queue per chat-stream handler. |
| `event_mapping.py`  | Pure mapping module — converts opencode's `/event` payloads (`message.part.delta`, `message.part.updated`, `permission.updated`) into `ThinkingEvent`s. No FastAPI, no I/O — independently testable. |
| `sse.py`            | Hand-rolled SSE wire framing (`format_event`, `format_keepalive`) plus `stream_with_keepalive`: a `shield`-based race wrapper that interleaves producer events with 15s idle keepalive comments. |
| `internal/`         | Reserved for plugin-only endpoints (e.g. fact injection during compaction). Empty today. |
| `migrations/`       | One `.sql` file per migration; applied in lex order; tracked by `_schema_migrations`. |
| `routes/`           | One `APIRouter` per logical endpoint group. `routes/__init__.py` declares the `ROUTERS` list — only routers in that list are mounted. |

## Lifespan: what happens at startup

`scufris_server/app.py:144` defines an `@asynccontextmanager` that
wraps the entire run. FastAPI calls it once at boot (yields control
to handlers) and again at shutdown.

The startup phase does ten things, in order:

1. **Apply migrations.** `store.apply_migrations()` walks
   `migrations/*.sql` in lex order and runs anything not in
   `_schema_migrations`. Each migration's DDL + tracking-row insert
   is one transaction. Failure here is fatal — wrong schema means
   nothing else works.

2. **Seed the default user.** Inserts `users(id=1, username='default')`
   if absent (`INSERT OR IGNORE`). The default user is what unknown
   `(surface, surface_id)` pairs fall back to (see
   [`003_identity_and_users.md`](003_identity_and_users.md)).

3. **Load `config.toml`.** Calls `identity.load_user_identity(path)`
   with `Settings.config_path`. Result is cached on
   `app.state.user_identity` as an `IdentityFile`. Always populated;
   missing file → empty `IdentityFile(user=None)`.

4. **Validate `SCUFRIS_USER_ID` override.** If set, looks up the id
   in `users`. Stale id → `RuntimeError`, which crashes the lifespan
   → uvicorn refuses to start. Validated id is cached on
   `app.state.identity_override`.

5. **Construct the opencode client.** Creates `OpencodeClient` with
   the configured URL + password. Cached on `app.state.opencode`.

6. **Probe `opencode /global/health`.** 30s budget. *Failure is not
   fatal* — server boots in degraded mode. The cache
   `app.state.opencode_initial_health` is set to the response on
   success or `None` on failure. `/v1/healthz` reports the current
   state on every call.

7. **Probe `opencode /provider`.** Only runs if the health probe
   succeeded. 30s budget. Picks the first connected provider with a
   default model, caches as `app.state.opencode_default_model`. On
   miss → `/v1/chat` 503s with `error_type=DefaultModelMissing`.

8. **Start the SSE event bus.** Constructs `EventBus` against the
   opencode client's underlying `httpx.AsyncClient`, spawns the
   long-lived reader task, and caches the bus on
   `app.state.opencode_event_bus`. The bus is started even on
   degraded boot — its reader auto-reconnects with exponential
   backoff (1s → 30s cap) so a missing-at-boot opencode that comes up
   later doesn't require a scufris restart. See
   [SSE event bus](#sse-event-bus) below.

9. **Yield.** uvicorn starts accepting requests.

10. **Shutdown** (in the `finally` block): stop the event bus
    *before* closing the opencode client, then close the client.
    Order matters — the bus's reader task uses the client's
    transport.

Every cached attribute on `app.state` is set inside the lifespan, so
handlers can read them via `request.app.state.X` knowing they're
populated.

## SSE event bus

`scufris_server/events.py` implements the one-bus-per-process model
prescribed by ADR-10. A single instance of `EventBus` is constructed
in lifespan step 8 and cached on `app.state.opencode_event_bus`.
Handlers that need to consume opencode events (today: just
`/v1/chat/stream`) call `EventBus.subscribe(session_id)` to get an
async-iterable queue of events scoped to that session.

The bus owns:

- **One long-lived `GET /event` SSE reader task**, regardless of how
  many `/v1/chat/stream` handlers are live. N concurrent streams ⇒
  1 upstream connection, not N (the "persistent consumer"
  invariant from ADR-10).
- **A reconnect loop with exponential backoff** — 1s initial delay,
  doubling each failure, capped at 30s. Used both at startup (bus is
  started even if opencode is down at boot) and mid-stream (if the
  upstream connection drops).
- **A per-session subscriber registry** — dictionary keyed by
  opencode session id, value is the list of `asyncio.Queue` instances
  registered by live handlers. The reader fans incoming events out to
  the matching subscribers; events for sessions with no live
  subscriber are dropped on the floor.

Two error contracts exposed to handlers:

- **`OpencodeUnavailable`** — raised by `subscribe()` if the bus has
  never successfully connected to opencode `/event` within the
  subscribe-timeout budget. This is the "bus never came up" failure
  mode. The chat-stream handler surfaces it as a pre-stream 503 with
  `error_type: "OpencodeUnavailable"`.

- **`_BusReconnected` sentinel** — when the bus reconnects after a
  drop (after at least one successful connection), it pushes a
  `_BusReconnected` instance into every live subscriber queue. The
  chat-stream handler treats receipt of this sentinel as a
  mid-stream failure and emits an `error` SSE event with
  `error_type: "BusReconnected"` (events that opencode emitted
  during the gap are lost, so the turn can't be trusted to have
  arrived intact). Replay-on-reconnect is §16.1 follow-up work.

The bus is documented in detail in its module docstring; the
mapping from opencode's wire events to `ThinkingEvent` lives in
`event_mapping.py`. See
[`002_api_reference.md` § POST /v1/chat/stream](002_api_reference.md#post-v1chatstream)
for the events as they appear on the wire to clients.

## Request flow: `POST /v1/chat` end-to-end

Walking through what happens between the curl going out and the
response coming back.

```
client                middleware             chat handler             opencode             sqlite
  │                       │                       │                      │                    │
  │── POST /v1/chat ─────►│                       │                      │                    │
  │                       │                       │                      │                    │
  │                       │ mint X-Request-Id     │                      │                    │
  │                       │ set REQUEST_ID_VAR    │                      │                    │
  │                       │ scope.state.req_id    │                      │                    │
  │                       │                       │                      │                    │
  │                       │── handler invoke ────►│                      │                    │
  │                       │                       │                      │                    │
  │                       │     pydantic ChatRequest validates body      │                    │
  │                       │                       │                      │                    │
  │                       │     resolve deps:                            │                    │
  │                       │       get_opencode_client()                  │                    │
  │                       │       get_db_conn()  ──────────────────────────────────► open WAL conn
  │                       │       get_user_identity()                    │                    │
  │                       │       get_identity_override()                │                    │
  │                       │                       │                      │                    │
  │                       │     check default_model cached ──┐           │                    │
  │                       │       missing? raise 503         │           │                    │
  │                       │                                  │           │                    │
  │                       │     resolve_user(conn, surface, ...) ──────────────────────────► SELECT/INSERT users
  │                       │       four-step algorithm        │           │                    │ INSERT surface_bindings
  │                       │       returns ResolvedUser       │           │                    │
  │                       │                                  │           │                    │
  │                       │     _resolve_session(conn, user_id, channel) ──────────────────► SELECT channels JOIN session_links
  │                       │                                  │           │                    │
  │                       │     no cached session?                       │                    │
  │                       │       client.create_session() ──►│ POST /session                  │
  │                       │       sessions.create_channel_link() ──────────────────────────► INSERT channels + session_links
  │                       │                                  │           │                    │
  │                       │     cached session?                          │                    │
  │                       │       _touch_session() ───────────────────────────────────────────► UPDATE session_links
  │                       │                                  │           │                    │
  │                       │     client.send_message() ───────►│ POST /session/:id/message     │
  │                       │                                  │  (blocks until LLM finishes)   │
  │                       │     ◄── AssistantMessage ────────│                                │
  │                       │                                  │           │                    │
  │                       │     ChatResponse(reply=text(), tokens=..., cost=...)              │
  │                       │                                              │                    │
  │                       │── http.response.start ────────────────────────────────────────────│
  │                       │     wrap_send adds X-Request-Id              │                    │
  │                       │                                              │                    │
  │ ◄── 200 + JSON body ──│                                              │                    │
                                                                                              │
                                                  on request exit: connect() closes the conn ─┘
```

### Key decisions visible in this flow

- **Identity resolution before session lookup.** The `(user_id, channel)`
  triple is the primary key for opencode-session reuse. Resolving the
  user first means a single `(surface, surface_id, agent)` always
  lands in the same opencode session across restarts, regardless of
  whether the binding was just materialised or read from cache. See
  `routes/chat.py:224`.

- **Session creation and message-send are separate calls** to
  opencode. We don't fold them together because (a) opencode's API
  treats them as separate operations, and (b) we need the
  `oc_session_id` written to SQLite *before* the message-send call so
  a crash mid-message doesn't leak the session.

- **DB transaction policy.** Each `with conn:` block is one atomic
  unit. The handler does *not* wrap the entire request in a single
  transaction — that would hold the writer lock across the (slow)
  `send_message` call. Instead, `sessions.create_channel_link` commits
  before we send, and `_touch_session` is a separate one-statement
  commit.

- **Error handling is selective.** `OpencodeNetworkError` and
  `OpencodeServerError` (5xx from opencode) → 503 with structured body.
  `OpencodeClientError` (4xx) is *not* caught — it's a bug in our
  request shape and we want it visible. See `routes/chat.py:23-29`.

## Dependency injection: why it's structured the way it is

`dependencies.py` is a separate module — not part of `app.py` —
specifically to break an import cycle:

- Routes need DI helpers (so they can `Depends(get_db_conn)`).
- `app.py` mounts the routes (so it imports them).
- DI helpers need `app.state` (so they read `request.app.state`).

If the helpers lived in `app.py`, `routes/*.py` would import `app.py`
which imports `routes/*` — circular. By extracting the helpers, the
import graph stays a DAG: `app.py → routes/* → dependencies.py`.

### What each helper does

```
get_opencode_client(request) -> OpencodeClient
    Returns app.state.opencode (set in lifespan step 5).

get_db_conn(request) -> AsyncIterator[sqlite3.Connection]
    Yields a fresh per-request connection. Opens at request start,
    closes at request end. async generator (not sync) so the connection
    stays on the event-loop thread that the handler runs on — sqlite
    pins connections to their opening thread by default.

get_user_identity(request) -> IdentityFile
    Returns app.state.user_identity (set in lifespan step 3).

get_identity_override(request) -> int | None
    Returns app.state.identity_override (set in lifespan step 4).
    None when SCUFRIS_USER_ID is unset.
```

`get_db_conn` is async-deliberately. FastAPI dispatches sync
generator dependencies on its threadpool, but async generators stay
on the event loop. SQLite's `check_same_thread=True` means a
threadpool-opened conn would be illegal to use from the async
handler that holds it. Going async sidesteps that entirely. Cost is
microseconds per request — WAL-mode SQLite opens are very cheap.

## What's *not* in v2 (today)

| Feature | Where it'll land |
|---------|------------------|
| Channel fork (`POST /v1/sessions/{id}/fork`) | `#33` (`tasks/20260616-111428`). Stub spot in `routes/sessions.py`. |
| Channel server-side expire (`DELETE /session/{id}` on opencode) | `#34` (`tasks/20260616-111430`). Distinct from today's `clear`, which preserves the upstream session per ADR-13. |
| Stats / per-user telemetry | `#13` (`tasks/20260613-091047`). Stub in `routes/stats.py`. |
| Permissions UX | `#30` (`tasks/20260613-093108`). Stub in `routes/permissions.py`. |
| In-tree opencode plugin | Future task. Touches `.opencode/plugin/scufris.ts`. |
| Bearer-token auth on `/v1/*` | `#14` will land alongside the v2 CLI. |
| Multi-tenant identity | Schema accommodates it (the `users` table is plural for a reason); UX/config doesn't. |
| Replay-on-reconnect for the SSE event bus | Filed during `#11` close-out as the §16.1 follow-up; today a mid-stream upstream drop surfaces as an `error` SSE event with `error_type: "BusReconnected"`. |

## What's *not* part of scufris (ever)

These are explicitly opencode's job:

- LLM model selection / context-window management.
- Agent personas (system prompts, model overrides, allowed tools).
- Tool execution (built-in or user-registered).
- Compaction (we contribute facts at compaction time via the plugin
  hook, but we don't run compaction).
- Permissions ("allow this tool call?"); we proxy and surface, not
  implement.

If you find yourself reaching into LLM territory inside `scufris_server/`,
stop and check `tasks/20260613-091036/TASK.md` §2 — non-goals — to
make sure you're not duplicating opencode.

## Cross-references

- API surface, field by field: [`002_api_reference.md`](002_api_reference.md)
- The identity resolver in detail: [`003_identity_and_users.md`](003_identity_and_users.md)
- Env vars + config files: [`004_configuration.md`](004_configuration.md)
- SQLite schema: [`005_data_model.md`](005_data_model.md)

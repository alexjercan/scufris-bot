# scufris-server — Design (v1)

> Output of the deployment spike (`tasks/20260510-192350`). Decisions
> here are inputs to the implementation tasks: HTTP server
> (`20260510-192505`), CLI client (`20260510-192636`), NixOS module
> (`20260510-192748`), and Home Manager module (`20260510-192825`).

## 1. Goals & non-goals

**Goals (v1):**

- One long-running daemon per host that owns the agent runtime
  (`AgentManager`, `ChatHistoryManager`, sub-agents, callbacks).
- Multiple concurrent clients (CLI today, Telegram next, future web UI)
  speak HTTP to the daemon.
- Deployable as a NixOS systemd service (root) or Home Manager
  `systemd.user.service`.
- Streaming "thinking" UX preserved 1:1 (CLI users see the same
  trace they see today).

**Non-goals (v1):**

- History persistence across restarts — agent state is in-process,
  lost on restart. (Tracked as a future task if needed.)
- TLS termination — assume reverse proxy (nginx, Caddy) when
  exposed beyond localhost.
- Multi-tenant identity / OAuth — `user_id` is opaque and trusted on
  localhost (see §6).
- Web UI — separate phase.

## 2. Transport: HTTP over loopback

- **HTTP, not Unix sockets.** Precedent: Ollama. Same model, same
  trust assumptions, and trivially extends to remote use later. Unix
  sockets save no meaningful complexity here and would require a
  bespoke client.
- **Default bind:** `127.0.0.1:8765`.
- **Remote use:** opt-in via `SCUFRIS_BIND=0.0.0.0` + a bearer token
  (`SCUFRIS_TOKEN`). Document that exposing beyond loopback assumes
  TLS-terminating reverse proxy.

## 3. Framework: FastAPI + uvicorn

Picked per spike comment. Tradeoffs:

- **Pros:** request validation via Pydantic, OpenAPI doc for free
  (useful when the web UI lands), ergonomic dependency injection,
  native async, well-known.
- **Cons:** heavier than Starlette (which it wraps). Acceptable —
  one extra dep, and we get the docs surface and DI for free.
- **SSE:** FastAPI ships `StreamingResponse`; `sse-starlette`
  (`EventSourceResponse`) is the pragmatic choice for proper SSE
  framing with keepalives. Add it as a dep.
- **ASGI server:** `uvicorn[standard]`. `--workers 1` is mandatory
  (see §5 — agent state is per-process; multi-worker would split
  history across processes).

## 4. Endpoint surface (v1)

All endpoints under `/v1/`. JSON request/response unless noted.

| Method | Path                | Body                                | Response                        | Notes                                     |
|--------|---------------------|-------------------------------------|---------------------------------|-------------------------------------------|
| GET    | `/v1/healthz`       | —                                   | `{"status":"ok"}`              | Liveness. 200 always when reachable.      |
| GET    | `/v1/readyz`        | —                                   | `{"status":"ok","ollama":true}` | Readiness — pings Ollama.                 |
| GET    | `/v1/version`       | —                                   | `{"version":"...","model":"..."}` | Build / config info.                    |
| POST   | `/v1/chat`          | `{user_id, message}`                | `{"reply":"..."}`              | Sync; returns final assistant text only.  |
| POST   | `/v1/chat/stream`   | `{user_id, message}`                | `text/event-stream`             | SSE; thinking events + terminal `done`.   |
| GET    | `/v1/stats`         | query: `user_id`                    | `{"rows":[...],"lines":[...]}` | Raw rows + pre-rendered lines.            |
| POST   | `/v1/clear`         | `{user_id}`                         | `{"cleared": <int>}`           | Wipes all per-(user, agent) slices.       |

### 4.1 Request schema

```json
POST /v1/chat
{
  "user_id": 1,
  "message": "what's the weather?"
}
```

`user_id` is an integer (matches today's `int` typing throughout
`history.py`). Strings are rejected. See §6 for identity discussion.

### 4.2 SSE event schema (`/v1/chat/stream`)

Each SSE message has an `event:` and a `data:` line. Payload mirrors
`utils.callbacks.ThinkingEvent` plus a terminal envelope:

```
event: thinking
data: {"kind":"text","source":"main","text":"...","depth":0,"arg":null,"context":null,"prior_turns":null,"evicted":null,"new_facts":null}

event: thinking
data: {"kind":"tool_call","source":"main","text":"knowledge_agent","depth":0,"arg":"weather in Bucharest","context":null,...}

event: thinking
data: {"kind":"compaction","source":"main","text":"","depth":0,"evicted":12,"new_facts":3,...}

event: done
data: {"reply":"It's 18°C and sunny."}

event: error
data: {"message":"...","type":"OllamaConnectionError"}
```

Rules:

- One `done` xor one `error` per stream, always last.
- Heartbeat comment (`: ping\n\n`) every 15s to keep proxies from
  closing the connection.
- Client disconnect cancels the in-flight agent task (see §5.4).

### 4.3 Stats response

Wrap `format_stats_lines()` plus the raw telemetry dict so clients
can either render the existing CLI table or build their own UI:

```json
{
  "lines": ["Agent          Msgs ...", "..."],
  "rows": {
    "knowledge_agent": {"messages": 42, "tokens": 1234, "budget": 8000, ...},
    "...": {...}
  }
}
```

## 5. Concurrency model

### 5.1 Process layout

- Single uvicorn worker, single asyncio event loop.
- One shared `AgentManager` + one shared `ChatHistoryManager`
  instantiated at startup (mirroring current `cli.py` /  `main.py`
  bootstrap).
- The agent itself (LangChain `Runnable`) is reused across requests.

### 5.2 Sync agent inside async handler

`AgentManager.process_message` is `async def`, but internally calls
`self.agent.invoke(...)` which is **synchronous** and CPU/IO-blocking
(LLM round-trips). Today this works because each CLI/Telegram process
only handles one request at a time.

In the server, blocking the loop would stall every other client.
Two options:

- **(A)** Switch to `agent.ainvoke(...)` (LangChain supports it). Best
  if it works end-to-end with our tools.
- **(B)** Wrap with `asyncio.to_thread(self.agent.invoke, ...)`. Safe
  fallback; uses a thread pool.

**Decision:** Try (A) first. If any tool/sub-agent breaks under
`ainvoke`, fall back to (B). The implementation task's first AC is
"two concurrent users don't block each other".

### 5.3 Per-user serialization

`ChatHistoryManager` mutates dicts without locking. Concurrent
requests for the **same** `user_id` would race on history mutations,
compaction, and telemetry counters. Different users are independent
(history is keyed by `(user_id, agent)`).

**Decision:** per-user `asyncio.Lock` map in the server layer:

```python
_user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

async def handle_chat(req):
    async with _user_locks[req.user_id]:
        return await agent_manager.process_message(...)
```

This keeps the history manager unchanged and pushes the policy
(serialize per user) to the boundary.

### 5.4 Per-request callbacks & SSE wiring

Today `ToolCallbackHandler` is constructed once and stored on
`AgentManager.callbacks`. For SSE we need a **fresh handler per
request** so each stream has its own `on_thinking` callback feeding
its own `asyncio.Queue`.

Refactor needed (small): allow callbacks to be passed *per
invocation*, not stored on the manager. Concretely, change
`AgentManager.process_message` to accept an optional
`extra_callbacks: list[BaseCallbackHandler]` and merge them into the
`config={"callbacks": [...]}` dict before invoking.

Server handler sketch:

```python
async def chat_stream(req):
    queue: asyncio.Queue[ThinkingEvent | _Done | _Err] = asyncio.Queue()

    def on_thinking(ev): queue.put_nowait(ev)
    handler = ToolCallbackHandler(on_thinking=on_thinking)

    async def run():
        try:
            reply = await agent_manager.process_message(
                messages, user_id=req.user_id,
                extra_callbacks=[handler],
            )
            await queue.put(_Done(reply))
        except Exception as e:
            await queue.put(_Err(e))

    task = asyncio.create_task(run())

    async def gen():
        try:
            while True:
                item = await queue.get()
                if isinstance(item, _Done):
                    yield {"event": "done", "data": json.dumps({"reply": item.reply})}
                    return
                if isinstance(item, _Err):
                    yield {"event": "error", "data": json.dumps({...})}
                    return
                yield {"event": "thinking", "data": json.dumps(asdict(item))}
        finally:
            task.cancel()  # client disconnected

    return EventSourceResponse(gen())
```

### 5.5 Compaction events routing

`ChatHistoryManager.set_event_sink()` accepts a **single** global
sink. In a multi-user server we need compaction events to flow to
the right SSE stream (the one for the user whose history just
compacted).

**Decision:** keep `set_event_sink` as the global sink, but the
sink dispatches on the *current request's* user via a `ContextVar`:

```python
_current_user: ContextVar[int | None] = ContextVar("current_user", default=None)
_user_sinks: dict[int, list[Callable]] = defaultdict(list)

def _global_sink(ev):
    uid = _current_user.get()
    if uid is None: return
    for cb in _user_sinks[uid]:
        cb(ev)
```

Each chat handler `set`s the context var and registers its callback
in `_user_sinks` for the duration of the request. Slightly fiddly but
keeps `history.py` unchanged.

Alternative considered: extend `set_event_sink` to take a `user_id`
filter or accept multiple sinks. Rejected for v1 — the ContextVar
approach localizes the change to the server module.

### 5.6 Graceful shutdown

- Trap `SIGTERM` (uvicorn does this for us via the lifespan event).
- Stop accepting new connections, wait up to **30s** for in-flight
  chats to finish. Match `TimeoutStopSec=35` in the systemd unit
  (5s margin).
- After grace period, cancel outstanding tasks; SSE clients see a
  `error` event with `type: "shutdown"` if we can flush, else a
  raw disconnect.

## 6. Identity & auth (v1)

- `user_id` is **opaque and trusted**: server takes whatever the
  caller sends.
- Localhost-only (default bind) → trust boundary is the OS user.
  Anyone with shell access on the host can already read process
  memory; bearer auth would be theatre.
- Remote bind (opt-in) → require `Authorization: Bearer
  $SCUFRIS_TOKEN` on every request. Single shared token in v1.
  Missing/bad token → 401.
- **v2 sketch (not built):** map token → `user_id` server-side, drop
  client-supplied `user_id` from the request body. Keeps the wire
  shape stable; only the binding changes.

## 7. Telegram bot migration

Two paths:

1. **Telegram becomes an HTTP client of the daemon.** `main.py`
   shrinks to: receive Telegram update → POST `/v1/chat/stream` →
   stream-coalesce thinking into typing actions → edit Telegram
   message with final reply. Single source of truth for the agent.
2. **Telegram keeps embedded agent.** Two processes, two histories,
   double the resident memory. Easier short-term, painful long-term.

**Decision:** target (1), but **not in v1**. Ship the server +
CLI client first; Telegram migration is a follow-up task to be
opened once the server is stable. Until then Telegram keeps its
current embedded form.

## 8. Process lifecycle & ops

- **Logging:** stdout (line-buffered), no Rich handler. The systemd
  unit captures into journald. Existing Python `logging` config is
  reused; the `RichHandler` is gated on `sys.stdout.isatty()` so
  TTY runs (developer `nix run`) still get color.
- **Ollama disconnect:** propagates as an exception → `error` event
  on the SSE stream / 502 on the sync endpoint. No automatic retry
  in the server; clients can retry.
- **Memory:** in-process only. Restarting the daemon wipes all
  histories. Documented; persistence is a separate task if needed.
- **Metrics:** out of scope for v1. `/v1/stats` covers per-user
  visibility; Prometheus etc. can layer on later via a middleware.

## 9. Configuration (env vars)

All config is env-driven, matching `utils/config.py` conventions.

| Var                    | Default            | Purpose                                      |
|------------------------|--------------------|----------------------------------------------|
| `SCUFRIS_BIND`         | `127.0.0.1`        | Listen address.                              |
| `SCUFRIS_PORT`         | `8765`             | Listen port.                                 |
| `SCUFRIS_TOKEN`        | (unset)            | Bearer token; if set, required on requests.  |
| `SCUFRIS_MODEL`        | (existing default) | Ollama model.                                |
| `SCUFRIS_OLLAMA_URL`   | (existing default) | Ollama endpoint.                             |
| `SCUFRIS_LOG_LEVEL`    | `INFO`             | Root log level.                              |
| `SCUFRIS_SHUTDOWN_GRACE` | `30`             | Seconds to drain in-flight chats on SIGTERM. |

Client-side (CLI) adds `SCUFRIS_SERVER_URL`, `SCUFRIS_USER`.

## 10. Module layout

New package, separate from existing modules so the CLI can import
the client cleanly without dragging in server deps:

```
scufris_server/
    __init__.py
    __main__.py        # `python -m scufris_server` entrypoint
    app.py             # FastAPI app factory
    routes/
        chat.py        # /v1/chat, /v1/chat/stream
        admin.py       # /v1/healthz, /v1/readyz, /v1/version
        stats.py       # /v1/stats, /v1/clear
    auth.py            # bearer dependency
    sse.py             # ThinkingEvent → SSE encoding helpers
    locks.py           # per-user lock map + event-sink ContextVar
    bootstrap.py       # builds AgentManager + history (reuses setup_scufris)
```

`pyproject.toml` adds `[project.scripts] scufris-server =
"scufris_server.__main__:main"`.

## 11. Open issues deferred to implementation

- Verify `agent.ainvoke` works with all current sub-agents and tools
  (§5.2). If not, document which one breaks and use `to_thread`.
- Decide concrete heartbeat interval and SSE chunk size — start with
  15s ping, revisit if proxies misbehave.
- Pydantic validation of `user_id` (positive int? bounded?) — depends
  on whether existing call sites assume `1` only. Leave permissive
  for v1.

## 12. References

- Existing: `cli.py`, `main.py` (bootstrap to mirror), `utils/agent.py`
  (AgentManager — needs per-call callbacks), `utils/callbacks.py`
  (`ThinkingEvent` — SSE payload), `utils/history.py`
  (`set_event_sink` — needs ContextVar shim), `utils/stats.py`
  (`format_stats_lines` for `/v1/stats`).
- Implementation tasks consuming this design:
  `tasks/20260510-192505` (server), `tasks/20260510-192636` (CLI
  client), `tasks/20260510-192748` (NixOS module),
  `tasks/20260510-192825` (Home Manager).
- FastAPI: https://fastapi.tiangolo.com/
- sse-starlette: https://github.com/sysid/sse-starlette
- Ollama HTTP API (precedent): https://github.com/ollama/ollama/blob/main/docs/api.md

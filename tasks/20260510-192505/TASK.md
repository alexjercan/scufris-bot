# Implement scufris HTTP server (daemon entrypoint)

- STATUS: CLOSED
- PRIORITY: 80
- TAGS: deploy, server

> Implements the design from
> `tasks/20260510-192350` (HTTP server architecture spike).
> Blocks on that task closing first so we don't churn the API
> surface mid-implementation.

## Goal

A new entrypoint `scufris-server` that runs the agent runtime as a
long-lived process and exposes it over HTTP. The CLI and
(eventually) Telegram bot become clients of this server. Single
binary, single config, suitable for `systemctl start scufris`.

## Scope

### In

- New module `scufris_server/` (or `server.py` if it stays small)
  containing:
  - ASGI app construction (framework picked in spike).
  - Endpoint handlers per the spike's endpoint table.
  - SSE streaming for `/v1/chat/stream` — bridge
    `ToolCallbackHandler.on_thinking` into an `asyncio.Queue`
    consumed by the SSE response.
  - Per-user `asyncio.Lock` map so concurrent requests for the
    same user serialise (history manager isn't internally
    thread-safe).
  - Shared singletons: history manager (with compactor + event
    sink), main agent, AgentManager, callback handler.
  - Graceful shutdown on SIGTERM (drain in-flight chats, bounded
    grace ~30s).
  - JSON-structured logging to stdout (journald-friendly).
- New entrypoint script: `pyproject.toml` adds
  `scufris-server = "scufris_server:main"` under
  `[project.scripts]`.
- Server-side config knobs (env-driven; mirror existing
  `utils/config.py` style): `SCUFRIS_BIND`, `SCUFRIS_PORT`,
  `SCUFRIS_TOKEN` (optional bearer for non-localhost binds).
- `/v1/healthz` does a lightweight Ollama ping (cached ~5s).

### Out

- TLS — handled by reverse proxy.
- Persistence across restarts — out of scope; document.
- Auth beyond a single shared bearer token — out of scope.
- CLI / Telegram client refactors — separate tasks.

## Acceptance criteria

- [ ] `scufris-server` starts, binds to 127.0.0.1:8765 by
      default, logs a startup line, and serves `/v1/healthz`.
- [ ] `POST /v1/chat` returns the assistant reply for a given
      `(user_id, message)`.
- [ ] `POST /v1/chat/stream` streams thinking events as SSE,
      followed by a terminal `done` event carrying the final
      reply text.
- [ ] `GET /v1/stats?user_id=N` returns the same data the CLI's
      `/stats` shows, in JSON.
- [ ] `POST /v1/clear` clears the user's history; returns count.
- [ ] Concurrent requests for *different* users run in parallel;
      requests for the *same* user serialise.
- [ ] SIGTERM drains in-flight requests within 30s, then exits 0.
- [ ] Bearer-token check rejects unauthenticated requests when
      `SCUFRIS_TOKEN` is set; allows everything when unset.
- [ ] No regressions in existing tests; new tests cover the
      endpoints (`httpx.AsyncClient` against the ASGI app, no
      real network).
- [ ] `ruff`, `pytest`, `mypy` clean.

## Implementation notes

- Bridge SSE via an `asyncio.Queue[ThinkingEvent | _Done]` per
  request. The `on_thinking` callback enqueues; the SSE generator
  drains. Tear down on client disconnect.
- Reuse `utils/stats.format_stats_lines` for the human format
  but expose the underlying `get_user_telemetry` dict directly
  on `/v1/stats` so clients can render their own way.
- Keep the framework dependency minimal — prefer Starlette over
  FastAPI unless the spike says otherwise.

## References

- Spike: `tasks/20260510-192350/TASK.md`.
- `cli.py` — pattern for wiring history manager + callbacks +
  agent manager.
- `main.py` — Telegram entrypoint to mirror lifecycle.
- `utils/agent.py` — `AgentManager.process_message` is already
  async; this is the per-request inner call.

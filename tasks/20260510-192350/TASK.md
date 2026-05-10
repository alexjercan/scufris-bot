# Deployment spike: HTTP server architecture for scufris daemon

- STATUS: OPEN
- PRIORITY: 90
- TAGS: deploy, design, server, spike

> Design spike — no implementation. Output is decisions + a design
> doc that the implementation tasks (`HTTP server`,
> `CLI as HTTP client`, `NixOS module`) consume.

## Goal

Decide the shape of a long-running scufris daemon that:

- hosts the agent runtime (LangChain agent, history manager,
  compactor, callbacks) once per host instead of per-CLI-process,
- serves multiple concurrent clients (a future web UI, the local
  CLI, and the existing Telegram bot),
- can be deployed as a systemd service (root or user) on NixOS via
  a module,
- is deliberately minimal in dependencies — no FastAPI/uvicorn
  unless we already need them, no auth framework unless secured
  endpoints exist.

## Open questions to answer

- **HTTP vs Unix socket vs both?** Ollama is HTTP-on-localhost,
  which is the closest precedent. HTTP also makes the future web
  UI / remote-from-laptop case trivial. Lean: HTTP, bound to
  127.0.0.1 by default, optional `bind = "0.0.0.0"` + token auth
  for remote use. Document the threat model: localhost = trusted,
  network = bearer token over HTTPS terminated by a reverse proxy.

- **Framework.** Options: stdlib `http.server` (too anaemic for
  SSE), `aiohttp` (already pulled in indirectly?), `starlette`
  (no FastAPI overhead, has SSE), `FastAPI` (more deps, more
  ecosystem), `uvicorn` raw. Recommend: starlette + uvicorn (or
  `hypercorn` for HTTP/2). Justify the pick.
  - comment: I would use `FastAPI` we can install it when needed

- **Streaming model for thinking events.** Server-Sent Events
  (one-way, simple, reconnect baked in) vs WebSockets (full
  duplex, harder to proxy) vs long-poll. SSE is the right fit:
  server pushes `ThinkingEvent`s, client only sends the next
  user message via a separate POST. Confirm.
  - comment: SSE sounds good

- **Endpoint surface (v1).** Sketch:
  - `POST /v1/chat` — body `{user_id, message}`; response is the
    final assistant text. SSE variant `POST /v1/chat/stream`
    streams thinking events first, then the final reply as a
    terminal event.
  - `GET /v1/stats?user_id=...` — returns `format_stats_lines()`
    output as JSON-serialisable rows (let the client format).
  - `POST /v1/clear` — body `{user_id}`; returns count cleared.
  - `GET /v1/healthz` — liveness / readiness (Ollama reachable).
  - `GET /v1/version` — build info.

- **Concurrency model.** One background event loop, one shared
  agent + history manager, async-safe. The current
  `ChatHistoryManager` has no locking; check whether per-(user,
  agent) keying is enough. If not, decide where to add an
  `asyncio.Lock` (per-user lock map probably).

- **User identity on the server side.** Today the CLI hardcodes
  `user_id=1` and Telegram uses the chat ID. Decide:
  (a) the server treats `user_id` as opaque and trusts the
  caller (fine for localhost, bad for shared use), or
  (b) the server requires a token → user_id mapping. Pick (a)
  for v1, document the upgrade path to (b).

- **How does the existing Telegram bot fit in?** Two options:
  1. Telegram bot becomes a *client* of the HTTP daemon (`main.py`
     shrinks to a Telegram→HTTP proxy).
  2. Telegram bot stays as-is, runs in a separate systemd unit,
     uses its own embedded agent.
  Pick (1) eventually, but call out that it can land in a
  follow-up — not blocking on the v1 server.

- **Process lifecycle.** Graceful shutdown on SIGTERM (drain
  in-flight chats; bounded grace). Ollama disconnect handling
  (already loose — model calls just fail). Memory of the agent
  across restarts: today everything is in-process and lost on
  restart. Decide: out-of-scope for v1 (document), or punt to a
  separate persistence task.

- **Logging.** Stdout for journald in the systemd unit; keep the
  Rich handler optional (TTY-only).

## Deliverables

A `DESIGN.md` (or extended notes section in this task) covering:

- Picked framework + justification.
- Endpoint table (method, path, body, response, streaming).
- SSE event schema (mirror `ThinkingEvent` JSON-serialised + a
  terminal `done` event with the final reply).
- Concurrency story (locks, async safety).
- Auth story for v1 (none/localhost) + v2 sketch.
- Telegram migration path.
- Out-of-scope list (persistence, multi-tenant, TLS termination).

## Out of scope

- Implementation (separate task).
- TLS — assume reverse proxy.
- Persistence of history across restarts — separate task if we
  ever need it.
- Web UI — separate later phase.

## References

- Existing entry points: `cli.py`, `main.py`.
- `utils/agent.py` (AgentManager — already async).
- `utils/callbacks.py` (`ThinkingEvent` — the streaming payload).
- Ollama HTTP API (precedent for localhost-by-default daemon).

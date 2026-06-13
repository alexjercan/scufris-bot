# scufris-server v2: HTTP daemon wrapping opencode serve

- STATUS: OPEN
- PRIORITY: 90
- TAGS: server,deploy,opencode

Implement the skeleton of `scufris-server` — the Python/FastAPI
daemon described in design doc §5.1. This task lands the *minimal
viable slice* that subsequent tasks fill in. Not the full §8 endpoint
table — just enough that the process boots, talks to opencode,
proves the chat loop end-to-end, and provides a stable foundation
for the dependent tasks.

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).
Implements: design doc `tasks/20260613-091036/TASK.md` §5.1, §6, §7
(schema only), §8 (subset), §9.1, §13 (skeleton), §14 (loopback only).

---

## Scope

### In

A working FastAPI daemon that:

1. Boots cleanly under uvicorn with a sensible lifespan (probe
   opencode at startup, stop cleanly on SIGTERM).
2. Owns one `httpx.AsyncClient` against `opencode serve` and one
   SQLite connection against `$XDG_STATE_HOME/scufris/scufris.sqlite`.
3. Applies the schema migrations from design §7 on startup
   (`apply_migrations()` step).
4. Exposes the **minimal** endpoint set:
   - `GET  /v1/healthz` — `{ok, opencode: {healthy, version}}`
   - `GET  /v1/version` — `{version, opencode_version}`
   - `POST /v1/chat`    — synchronous chat per design §9.1, with a
     hard-coded `user_id=1` for v0 (real identity resolution is #12).
5. Logs structured-ish JSON to stdout with request IDs (full
   structured-logging story is #28; this task lays the hook so #28
   doesn't need to retrofit).

### Explicitly out (delegated)

| Endpoint / feature                         | Owning task                                      |
|--------------------------------------------|--------------------------------------------------|
| `/v1/identity/resolve`                     | #12 (`20260613-091046`) Identity layer           |
| `/v1/chat/stream` (SSE)                    | #11 (`20260613-091045`) SSE streaming            |
| `/v1/sessions` list / fork / clear         | #10 (`20260613-091044`) Per-user session mgmt    |
| `/v1/stats`, `/v1/clear`                   | #13 (`20260613-091047`) Stats/clear              |
| `/v1/permissions/:perm_id/reply`           | #30 (`20260613-093108`) Permissions UX bridge    |
| `/v1/internal/*` plugin endpoints          | #32 (`20260613-093857`) Plugin protocol          |
| Bearer-token auth for non-loopback         | #14 (`20260613-091048`) Auth                     |
| Per-message cost aggregation, /metrics     | #28 (`20260613-091103`) Observability            |
| Persistent SSE consumer task               | #11 (this task only stubs the module)            |

This task should leave `routes/`, `events/`, `internal/` etc.
*module placeholders* so the dependent tasks don't have to argue
about layout.

---

## Module layout

Land the package skeleton:

```
scufris_server/
├── __init__.py          # version constant
├── __main__.py          # `python -m scufris_server` → uvicorn entry
├── app.py               # FastAPI app factory + lifespan
├── config.py            # env-var loading: SCUFRIS_PORT, SCUFRIS_BIND,
│                        # OPENCODE_URL, OPENCODE_PASSWORD, etc.
├── opencode_client.py   # httpx.AsyncClient wrapper, typed methods
├── store.py             # sqlite3 connection + apply_migrations()
├── migrations/
│   └── 001_initial.sql  # schema from design §7
├── routes/
│   ├── __init__.py      # routers aggregated here
│   ├── health.py        # /v1/healthz, /v1/version
│   ├── chat.py          # /v1/chat (sync only in this task)
│   ├── identity.py      # placeholder: 501 Not Implemented (for #12)
│   ├── sessions.py      # placeholder (for #10)
│   ├── stats.py         # placeholder (for #13)
│   └── permissions.py   # placeholder (for #30)
├── events.py            # SSE consumer skeleton (no-op in this task; #11 fills in)
├── internal/
│   └── __init__.py      # placeholder (for #32)
├── identity.py          # placeholder (for #12)
└── logging.py           # request-ID middleware + structured stdout logger
```

Tests:

```
tests/
├── conftest.py                  # FastAPI test client fixture, sqlite tmp dir
├── unit/
│   ├── test_config.py           # env-var loading
│   ├── test_store.py            # apply_migrations() + basic CRUD
│   ├── test_opencode_client.py  # against respx mock transport
│   ├── test_logging.py          # request-ID middleware
│   ├── test_health.py           # /v1/healthz with opencode mocked up + down
│   └── test_chat_sync.py        # POST /v1/chat against a stubbed opencode
├── integration/
│   ├── conftest.py              # real-stack fixtures (see below)
│   └── test_chat_real.py        # one end-to-end pass through real opencode + ollama
└── support/
    └── fake_opencode.py         # in-process httpx mock transport
```

**Two test layers.**

- **Unit tests** (`tests/unit/`) — fast, deterministic, no external
  processes. opencode HTTP is stubbed via `respx`. Run on every
  push, in `nix flake check`, in CI.
- **Integration test** (`tests/integration/`) — at least one test
  that drives the real chat loop end-to-end against a running
  `opencode serve` plus `ollama` with `qwen3:latest`. Marked
  `@pytest.mark.integration` and skipped by default; opt in with
  `pytest -m integration` (or env var
  `SCUFRIS_INTEGRATION_TESTS=1`). The fixture probes
  `OPENCODE_URL/global/health` and the model's presence in
  `/provider`; if either is unreachable the test is *skipped*, not
  *failed*. CI invocation is left to a follow-up gate (e.g. a
  separate nightly job once #29 / perf baseline lands); `pytest`
  default stays green offline.

---

## TODOs (in implementation order)

Each item is small enough to land in one commit and be reviewed independently.

1. **Project scaffolding.**
   - [x] `pyproject.toml` with `fastapi`, `uvicorn[standard]`, `httpx`,
         `pydantic>=2`, `pytest`, `pytest-asyncio`, `respx` (or
         equivalent for httpx mocking). Use `uv` to manage.
   - [x] `scufris_server/__init__.py` exposes `__version__`.
   - [x] `scufris_server/__main__.py` runs uvicorn against
         `scufris_server.app:create_app`, reading `bind`/`port` from
         `Settings` (i.e. honoring `SCUFRIS_BIND` / `SCUFRIS_PORT`).

2. **Config (`config.py`).**
   - [x] Read env vars: `SCUFRIS_BIND` (default `127.0.0.1`),
         `SCUFRIS_PORT` (default `7080`), `OPENCODE_URL` (default
         `http://127.0.0.1:4096`), `OPENCODE_SERVER_PASSWORD` (no
         default; warn at startup if unset and the URL is non-loopback),
         `SCUFRIS_STATE_DIR` (default `$XDG_STATE_HOME/scufris`,
         falling back to `~/.local/state/scufris`).
   - [x] Pydantic `Settings` model; one instance per process.
   - [x] Tests cover env-var override and defaults.

3. **Storage (`store.py` + `migrations/`).**
   - [x] `apply_migrations(conn)` runs each `.sql` file in
         `migrations/` exactly once, tracked in a
         `_schema_migrations` table.
   - [x] `001_initial.sql` contains *exactly* the schema in design §7
         (users, surface_bindings, channels, session_links, facts,
         event_log) — copy verbatim, no fields invented.
   - [x] WAL mode set on connection.
   - [x] Provide `get_conn()` + a context-manager helper. Threadsafe
         for FastAPI's async — open one connection per request via a
         dependency, or a thread-local / per-task variant. Pick the
         pattern; document the choice in a module docstring.
   - [x] Tests: migrations idempotent; basic insert/select on each
         table.

4. **opencode client (`opencode_client.py`).**
   - [x] Single `OpencodeClient` class wrapping `httpx.AsyncClient`.
         Auth via HTTP basic with empty username + the configured
         password.
   - [x] Typed methods (Pydantic models):
         - `health() -> {healthy, version}`
         - `create_session() -> Session`
         - `send_message(session_id, request: SendMessageRequest) -> AssistantMessage`
   - [x] Error model: distinct exceptions for 4xx vs 5xx vs network;
         `OpencodeUnavailable` for `/global/health` failure.
   - [x] Lifespan integration: client opened in app startup, closed
         on shutdown.
   - [x] Tests use `respx` (or `httpx.MockTransport`) to stub
         responses. Cover happy path and 503-from-opencode.

5. **App factory & lifespan (`app.py`).**
   - [x] `create_app() -> FastAPI` builds the app.
   - [x] Lifespan:
         - Open SQLite connection, run `apply_migrations`.
         - Create `OpencodeClient`.
         - Probe `OpencodeClient.health()` with a 30s timeout; log
           result. Failure is *not* fatal (server still boots in
           degraded mode; `/v1/healthz` reports it).
         - On shutdown: close client, close SQLite.
   - [x] Mount routers from `scufris_server.routes`.
   - [x] Test boot/teardown cycle.

6. **Logging (`logging.py`).**
   - [x] Request-ID middleware: generate a ULID per incoming request,
         attach to `request.state.request_id` and to a contextvar so
         downstream code can log it.
   - [x] Stdout logger that emits JSON lines with: ts, level, request_id,
         msg, plus arbitrary kwargs. Keep dependency-free (no
         structlog yet — that lives in #28).
   - [x] Tests: middleware sets the contextvar; log line contains
         the request ID.

7. **Routes — `health.py`.**
   - [x] `GET /v1/healthz` calls `OpencodeClient.health()`. Returns
         `{ok: bool, opencode: {healthy, version} | {error: str}}`.
         200 even when opencode is down — this endpoint is for
         scufris's own liveness; opencode status is reported as data.
   - [x] `GET /v1/version` returns `{version: __version__,
         opencode_version}`. opencode_version pulled lazily; cached
         after first successful probe.
   - [x] Tests: opencode up, down, version cached.

8. **Routes — `chat.py` (sync only).**
   - [x] Request model: `ChatRequest{message: str, channel: {surface,
         surface_id, agent}}`. `user_id` is hard-coded to `1` for v0
         (real resolution lives in #12).
   - [x] Per design §9.1:
         - Resolve `(user_id=1, channel) → oc_session_id` from
           `channels`/`session_links`.
         - If absent: `OpencodeClient.create_session()`, insert rows.
         - Call `OpencodeClient.send_message(...)` with the configured
           default model (read from opencode `/config` at app startup
           and cached) and agent from the request.
         - Return `{reply, oc_session_id, oc_message_id, tokens, cost}`.
   - [x] Unit tests (mocked opencode):
         - First call creates session and link rows.
         - Second call to the same channel reuses the session.
         - opencode 503 → 503 from `/v1/chat` with a structured error.

9. **Integration test against real opencode + ollama.**
   - [ ] `tests/integration/conftest.py` provides:
         - A fixture that requires `OPENCODE_URL` (default
           `http://127.0.0.1:4096`) reachable; `pytest.skip()` if not.
         - A fixture that requires the configured model (default
           `ollama/qwen3:latest`) to appear in `GET /provider`'s
           connected providers; skip otherwise.
         - A fresh SQLite tmp dir per test run.
   - [ ] `tests/integration/test_chat_real.py` exercises one full pass:
         - Boot `scufris_server` via the test client.
         - `POST /v1/chat` with a tiny prompt that doesn't need tools
           (e.g. system prompt restricting to a one-word reply).
         - Assert reply is non-empty, `oc_session_id` is populated,
           `tokens.input + tokens.output > 0`.
         - Send a follow-up to the same channel; assert the
           `oc_session_id` is identical (session reuse).
   - [ ] Mark with `@pytest.mark.integration`. `pyproject.toml`
         registers the marker.
   - [ ] Document in README how to run integration tests
         (prerequisites: ollama up with qwen3 pulled, `opencode serve`
         on default port).

10. **Placeholder routers.**
   - [ ] `identity.py`, `sessions.py`, `stats.py`, `permissions.py`,
         `internal/__init__.py`, `events.py` exist as empty modules
         (or with a single `router` that 501s on every path) so the
         dependent tasks have a place to land.
   - [ ] Each carries a docstring naming the owning task ID.

11. **README / quickstart.**
    - [ ] Top-level `README.md` (or `docs/server.md`) section: how to
          run `opencode serve` + `scufris-server` locally; how to
          curl `/v1/healthz` and `/v1/chat`.
    - [ ] Separate "Running integration tests" section: ollama
          prerequisites, `pytest -m integration` invocation, expected
          runtime (single-digit seconds against qwen3:latest on a
          reasonable local model).

---

## Acceptance criteria

- [x] `python -m scufris_server` starts on `127.0.0.1:7080`, prints
      `opencode healthy=True version=X.Y.Z` (or `False` if down) within 2s.
- [x] `curl localhost:7080/v1/healthz` returns 200 with the shape above.
- [x] `curl -XPOST localhost:7080/v1/chat -d '{...trivial prompt...}'`
      returns the assistant reply, with `oc_session_id` populated.
- [x] Subsequent requests to the same `(surface, surface_id, agent)`
      tuple reuse the same opencode session (verified in DB and via
      `info.parentID` chain).
- [ ] `pytest tests/unit/` is green offline (no opencode, no ollama).
- [ ] `pytest -m integration` is green when run with
      `opencode serve` + `ollama` (`qwen3:latest`) on default ports.
      Single integration test passes in <30s.
- [ ] No lint errors with `ruff check`. Type-check clean with
      `mypy --strict scufris_server`.
- [ ] All placeholder modules referenced by dependent tasks
      (#10–#13, #28, #30, #32) exist and import without error.

---

## Dependencies & sequencing

- **Hard-blocks**: this task. Once it lands, the following can start
  in parallel:
  - #10 (session mgmt) — fills in `routes/sessions.py`
  - #11 (SSE streaming) — fills in `events.py` + `routes/chat.py` stream variant
  - #12 (identity) — fills in `identity.py` + `routes/identity.py`
  - #28 (observability) — replaces `logging.py` skeleton with structured logs + /metrics
  - #30 (permissions) — fills in `routes/permissions.py`
- **Soft-depends-on**: nothing. This task can start as soon as the
  design doc (which it does — `20260613-091036` is CLOSED).

---

## Open questions

1. **SQLite connection model under FastAPI async.** Open one
   connection per request via a dependency, one per task via
   contextvar, or a single shared connection serialised through a
   queue? Pick during step 3 and document the choice. Prior art:
   FastAPI docs recommend per-request for safety. Single-writer
   semantics aren't a problem at this scale.
2. **`uv` vs. `pip` for v0.** Planning notes lean `uv`. Confirm
   during step 1 — minor decision but locks the lockfile format.
3. **Should `/v1/chat` accept multipart parts** (text + file) **in this
   task, or text-only?** Design §9.1 supports the full Part union but
   v0 only needs text. Recommend text-only here; broaden when a
   real client needs it. Confirm during step 8.

These are deliberately *small* decisions left to implementation.
None block starting the task.

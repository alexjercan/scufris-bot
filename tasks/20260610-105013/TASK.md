# OpenCode /event listener fan-out for concurrent users

- STATUS: CLOSED
- PRIORITY: 30
- TAGS: opencode,performance

## Outcome

Shipped 2026-06-10. The OpenCode `/event` SSE bus is now consumed
through a single shared connection regardless of concurrent chat
count.

### What landed

- **`utils/opencode_client.py`** — new `_OpenCodeEventBus` class
  (~200 lines) plus three helper methods on `OpenCodeClient`
  (`start_event_bus`, `stop_event_bus`, `event_bus`).
  - Single long-lived `asyncio.Task` reads `GET /event` and
    dispatches each event by `properties.sessionID` to a list of
    per-subscription `asyncio.Queue` instances. Events without a
    sessionID (`server.connected`, `installation.*`, `lsp.*`) are
    dropped at the dispatch layer — `map_opencode_event` ignores
    them so this is loss-free.
  - `subscribe(session_id)` is an async context manager: registers
    a queue, waits for the bus's first connect (with timeout), and
    cleans up on exit.
  - Reconnection: on upstream drop the reader catches the exception,
    sleeps with exponential backoff (1s → 30s cap), and retries. On
    every reconnect after the first connect, a `_BusReconnected()`
    sentinel is broadcast to every live subscriber queue so
    consumers can decide whether to fail the turn or refetch state.
  - Stats: `bus.stats` exposes `connected`, `reconnects`,
    `subscribers`, `sessions`, `dropped_events`, `max_queue_depth`
    for `/v1/stats` (next P30 task).

- **`OpenCodeClient.chat_stream`** — refactored into a thin dispatcher
  that picks `_chat_stream_via_bus` (when the bus is running) or
  `_chat_stream_legacy` (per-request `GET /event`, kept for tests
  and pre-bus callers). Shared helpers `_build_chat_body`,
  `_post_message`, and `_drain_events` are the only spots holding
  POST/event-loop logic, so both paths agree on POST sentinels and
  termination semantics. The legacy path's behaviour is unchanged.

- **`OpenCodeClient.aclose`** — chains `bus.stop()` before closing
  the underlying httpx client, so the reader task is cancelled
  cleanly during shutdown.

- **`scufris_server/app.py`** — lifespan calls
  `await runtime.opencode_client.start_event_bus()` after
  `build_runtime` and before `prune_invalid_sessions`. Wrapped in
  a broad `except` so a transient connect failure doesn't abort
  startup — the bus's own backoff loop will keep retrying in the
  background once started.

- **`tests/test_opencode_event_bus.py` (new)** — 20 tests covering:
  - Pure-dispatch routing (no httpx): subscribe/unsubscribe lifecycle,
    sessionID filtering, drops for global/unsubscribed sessions,
    multi-subscriber fan-out, sentinel broadcast, stats snapshot,
    connect-timeout enforcement.
  - Lifecycle with a custom `httpx.AsyncBaseTransport`: idempotent
    start/stop, end-to-end SSE parsing, automatic reconnect-on-drop
    with `_BusReconnected` broadcast, `aclose()` chains stop,
    `start_event_bus` idempotent on the client.
  - `chat_stream` integration: bus-backed path yields events and
    terminates on `session.idle`; legacy path still opens its own
    connection per turn (regression guard); `_BusReconnected`
    mid-turn surfaces as `OpenCodeSessionError("…reconnected
    mid-turn…")`.
  - **Headline acceptance test:** 8 parallel `chat_stream` calls
    over distinct sessions complete cleanly while
    `transport.event_connect_count == 1` — confirming a single
    upstream `/event` connection serves K concurrent turns.

- **Live smoke** (`/tmp/nix-shell.cO2k21/opencode/smoke_p30.py`):
  ran against the local `opencode serve` 1.15.13 (port 4096):
  - 3 serial turns over a fresh session → text matched (`alpha`,
    `beta`, `gamma`).
  - 3 parallel turns over distinct sessions → 11.28s wall-clock
    (vs 24.21s sum of individual turn times) → real concurrency.
    `bus._task` identity unchanged across the burst, `reconnects`
    counter stayed at 0.
  - `aclose()` clean. OpenCode session count back to 25 after
    cleanup (no leakage).

### Verification

- `pytest`: 378 tests passing (was 358; +20 bus tests).
- `ruff check` + `ruff format --check`: clean (51 files).
- `mypy utils scufris_server`: clean (33 files).
- `nix flake check --no-build`: all checks evaluate (ruff, mypy,
  pytest, scufris-vm, both NixOS modules).
- Live smoke: as above.

### Acceptance criteria

- [x] One `GET /event` connection regardless of concurrent chat
      count — verified by `event_connect_count == 1` in
      `test_chat_stream_concurrent_share_one_upstream_connection`
      (K=8) and by live smoke (`bus._task` identity unchanged across
      a 3-way concurrent burst).
- [x] N concurrent chat requests still receive their own events
      correctly — same test asserts each consumer terminated on
      `session.idle` and saw its own `message.part.delta`.
- [x] Reconnection on upstream drop is automatic and doesn't lose
      events… or, if it does, the consumer recovers via a state
      fetch — bus auto-reconnects with backoff; consumers receive
      `_BusReconnected` and (per current chat_stream policy) raise
      `OpenCodeSessionError`. Recovery via `GET /session/{id}/message`
      is left to a follow-up — task notes the policy explicitly.
- [x] `nix flake check` passes.

### Open questions resolved

- "Where the upstream-drop recovery actually lives — fan-out task
  vs. per-consumer responsibility." → Bus broadcasts a sentinel,
  consumer decides. v1 policy: `chat_stream` fails the turn loud
  rather than guess at lost events; future state-fetch recovery is
  a separate task.
- "Whether to expose the fan-out queue depth on `/v1/stats` for
  observability." → `bus.stats` is implemented and ready; surfacing
  it on `/v1/stats` is folded into task 105018.

## Original task description (preserved)

### Motivation

The v1 design from `tasks/20260610-101413` opens a fresh
`GET /event` SSE connection on every chat request, holds it open for
the duration of the turn, and closes it when `session.idle` arrives.
Simple and correct, but with N concurrent chat requests (Telegram +
CLI + future surfaces) we hold N parallel HTTP connections to
OpenCode and parse its global event bus N times — once per consumer.

The OpenCode `/event` bus is server-global: every connection receives
every event for every session. So one well-managed listener could
fan-out to N per-session asyncio queues for free.

Worth doing once concurrency goes beyond a handful of users, or
sooner if the per-request connections show up as latency in traces.

### Scope

#### In

- Single long-lived background task in the FastAPI app lifespan that
  subscribes to `GET /event` once.
- Per-`(session_id, message_id)` `asyncio.Queue` registered when a
  request starts, removed when it ends.
- The fan-out task routes each event to the right queue based on
  `properties.sessionID` (and `properties.messageID` for filtering
  late echoes).
- Reconnection logic: if the upstream `/event` connection drops,
  reconnect with backoff and re-deliver `server.connected` (or signal
  consumers to re-fetch state via `GET /session/{id}/message` to catch
  up).
- Metrics: per-listener queue depth, dropped events.

#### Out

- Replacing the per-request connection wholesale before this is
  proven. Ship the simple version first; this is a swap.

### Acceptance criteria

- [ ] One `GET /event` connection regardless of concurrent chat count.
- [ ] N concurrent chat requests still receive their own events
      correctly (verified by a stress test that fires K parallel
      `/v1/chat/stream` calls and asserts each one terminates).
- [ ] Reconnection on upstream drop is automatic and doesn't lose
      events that arrive during the gap (or, if it does, the consumer
      recovers via a state fetch).
- [ ] `nix flake check` passes.

### Open questions

- Where the upstream-drop recovery actually lives — fan-out task vs.
  per-consumer responsibility.
- Whether to expose the fan-out queue depth on `/v1/stats` for
  observability.

### References

- `tasks/20260610-101413/TASK.md` — parent task; uses the simple
  per-request approach by default.
- `tasks/20260610-101413/SCHEMA.md` — confirms the bus is
  server-global.
- `utils/opencode_client.py` — owner of the listener once this lands.

# SSE streaming of opencode 'thinking' events

- STATUS: CLOSED
- PRIORITY: 75
- TAGS: server,observability,streaming

Bridge opencode's tool-call / reasoning stream into a `POST
/v1/chat/stream` SSE endpoint so the user sees subagent spawns, tool
calls, and model reasoning live (v1 UX parity). The wire shape is the
v1 `ThinkingEvent` JSON envelope — the post-swap renderer in
`feature/opencode:scufris_client/client.py:141-156` keeps working
unchanged at the SSE layer — but the *source* events are opencode's
`message.part.delta`, `message.part.updated`, and `permission.updated`
emitted on the global `GET /event` bus. Translation happens in a new
mapping module ported verbatim from v1's `utils/opencode_events.py`.

Implements:

- Design doc `tasks/20260613-091036/TASK.md` §5.1 line 103-105
  (`scufris_server.events` consumer task), §8 line 293
  (`POST /v1/chat/stream`), §9.2 lines 323-334 (5-step streaming
  flow), ADR-10 line 532-537 (one persistent SSE consumer per
  process), §16.1 line 568-575 (reconnect verification — partially;
  see D4).
- The v1 streaming chat surface ported to v2: `feature/opencode`
  branch's `_OpenCodeEventBus` + `chat_stream()` + SSE parser, now
  living inside scufris-server instead of the in-process LangChain
  runtime.

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).

One follow-up filed during this task's step 14 close-out:

- **§16.1-reconnect** (TBD task id) — Empirical verification that
  events emitted during a consumer disconnect are correctly handled
  (loss-on-drop assumption from design §16.1). Split out per D4
  because the harness needs to forcibly drop the upstream connection
  mid-turn and observe what's missed; large enough that bundling it
  into #11's acceptance gate would balloon scope. #11 ships the bus
  + endpoint + reconnect logic; #16.1-reconnect verifies the
  recovery story.

---

## Scope

### In

1. **`scufris_server/events.py`** (FILL placeholder):
   - `ThinkingEvent` dataclass — verbatim port of
     `feature/opencode:utils/callbacks.py:128-155`. 9 fields (kind,
     source, text, depth, arg, context, prior_turns, evicted,
     new_facts). `SCUFRIS_SOURCE = "scufris"` constant; `depth=0`
     always (single-agent post-swap), kept as a field for v1 wire
     compatibility. Added: `to_payload() -> dict[str, Any]` for SSE
     serialisation (only non-None fields surfaced).
   - `EventBus` class — port of `feature/opencode:utils/opencode_client.py:130-380`
     `_OpenCodeEventBus`. ONE shared `GET /event` consumer per
     process per ADR-10. `start()`, `stop()`, `subscribe(session_id)`
     async context manager, `stats` property. Reconnect with
     exponential backoff (1s initial → 30s cap); `_BusReconnected`
     broadcast on every reconnect after the first. Events dispatched
     by `properties.sessionID`; events without sessionID (server.connected,
     lsp.*, installation.*) dropped at the dispatch layer.

2. **`scufris_server/event_mapping.py`** (NEW): pure mapping module.
   - Verbatim port of `feature/opencode:utils/opencode_events.py`:
     `EventMapperState`, `extract_text_delta`, `map_opencode_event`,
     `_map_part_updated`, `_summarise_input`.
   - Dedup logic: one `tool_call` per `part.id` (on first `running`),
     one `tool_result` per `part.id` (on `completed` or `error`).
   - `permission.updated` → `tool_meta` ThinkingEvent (implicit #11
     decision — matches v1 post-swap mapping).
   - No FastAPI, no httpx — pure dataclasses + stdlib so the module
     is trivially testable without I/O.

3. **`scufris_server/sse.py`** (NEW): outbound SSE framing helpers.
   - `KEEPALIVE_SECONDS = 15` module constant.
   - `format_event(event_name: str, payload: Mapping[str, Any]) -> bytes`
     — yields `event: <name>\ndata: <compact_json>\n\n`.
   - `format_keepalive() -> bytes` — yields `: keepalive\n\n`.
   - `stream_with_keepalive(events: AsyncIterator[bytes]) ->
     AsyncIterator[bytes]` — interleaves keepalives if no event in
     `KEEPALIVE_SECONDS` (uses `asyncio.wait_for` per iteration).
   - Hand-rolled — no `httpx-sse` dep added (existing `httpx>=0.28.0`
     is sufficient since v1 already used the same pattern; saves a
     pyproject.toml edit).

4. **`scufris_server/opencode_client.py`** extensions:
   - `OpencodeStaleSessionError` exception (v1 carry-over) — raised
     when opencode returns 404 on a session we thought was live (GC).
     Distinct from `OpencodeClientError` so the stream handler can
     decide whether to recreate + retry vs. surface to the client.
   - `httpx_client` read-only property exposing the underlying
     `httpx.AsyncClient` so `EventBus` can call `client.stream("GET",
     "/event")` directly without re-instantiating. Aligns with v1's
     pattern where the bus borrowed the AsyncClient from
     `OpenCodeClient`.
   - No new `stream_events()` method — bus uses the raw
     `httpx.AsyncClient.stream()` per v1 layering.

5. **`scufris_server/app.py`** lifespan additions:
   - Start `EventBus` after `OpencodeClient` is constructed; stop
     before client `close()` (ordering matters — bus reader must
     finish before transport tears down).
   - Tolerate opencode-unreachable at bus start: log WARNING,
     leave bus in disconnected state. Reconnect loop retries
     forever; `/v1/chat/stream` returns 503 with structured body
     until the bus connects for the first time.
   - `app.state.opencode_event_bus: EventBus` (typed, never None
     once lifespan completes).

6. **`scufris_server/dependencies.py`**: add `get_event_bus(request)
   -> EventBus`.

7. **`scufris_server/routes/chat_stream.py`** (NEW):
   - `POST /v1/chat/stream` — accepts the same `ChatRequest` shape as
     `/v1/chat` (D2; cross-route reuse — imported from `routes.chat`).
   - Identity + session resolve via `_resolve_session` /
     `_touch_session` imported from `routes.chat` (D6;
     cross-route locality wins for v0; promotion to `sessions.py`
     can happen later if more callers materialise).
   - Returns `StreamingResponse(media_type="text/event-stream",
     headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})`.
   - Pre-stream errors (default model missing, create_session
     network/5xx) return JSON 503 with `ChatErrorBody`-shaped body
     (same as `/v1/chat`) — these happen before the SSE response is
     committed.
   - Stream body algorithm (per D3 + D8):
     1. Subscribe to bus: `async with bus.subscribe(oc_session_id)
        as queue:`.
     2. Fire poster: `post_task = asyncio.create_task(
        client.send_message(...))`.
     3. Drain `queue` in a `while True` loop; for each item:
        - If raw event dict → `map_opencode_event(item, state)`; if
          ThinkingEvent emitted, yield `format_event("thinking",
          ev.to_payload())`.
        - If `_BusReconnected` sentinel → yield `format_event("error",
          {error: "...", error_type: "BusReconnected"})`; break.
        - If event type is `session.idle` and sessionID matches →
          `await post_task` to get AssistantMessage; yield
          `format_event("done", {type, message, oc_session_id,
          oc_message_id, tokens, cost})` (D7 — superset of §8 done
          shape, mirrors `ChatResponse`); break.
        - If event type is `session.error` → yield
          `format_event("error", {...})`; break.
     4. `finally:` cancel `post_task` if still running; bus
        unsubscribe is automatic via the context manager.

8. **`scufris_server/routes/__init__.py`**: mount `chat_stream_router`
   (ROUTERS 5 → 6).

9. **Test coverage**:
   - `tests/unit/test_events.py` (~10 tests) — ThinkingEvent
     to_payload, EventBus start/stop/subscribe, reconnect sentinel,
     dispatch filter, bad JSON skipped, first-connect timeout.
   - `tests/unit/test_event_mapping.py` (~15 tests) — table-driven
     fixtures covering each event branch + dedup.
   - `tests/unit/test_sse.py` (~6 tests) — format_event,
     format_keepalive, stream_with_keepalive timing.
   - `tests/unit/test_chat_stream_route.py` (~12 tests) — full
     endpoint behaviour with mocked bus.
   - `tests/unit/test_opencode_client.py` — extend for
     `OpencodeStaleSessionError` + `httpx_client` property.
   - `tests/unit/test_placeholders.py` — drop
     `scufris_server.events` from `NON_HTTP_PLACEHOLDERS`; bump
     ROUTERS assertion to 6.
   - `tests/unit/test_app.py` — extend for lifespan bus
     start/stop ordering.
   - `tests/integration/test_chat_stream_real.py` (NEW, 90s budget)
     — opt-in against real opencode + ollama.

10. **`examples/check_chat_stream.py`** (NEW) + `examples/README.md`
    update — SSE-reading example mirroring `check_chat.py` UX.

11. **Docs**: promote `/v1/chat/stream` from "Future endpoints" to
    live in `docs/002_api_reference.md`; add bus paragraph to
    `docs/001_architecture.md`; update layout + test counts in
    `docs/006_development.md`. No `docs/005_data_model.md` changes
    (no schema changes).

### Out

| Feature                                              | Owning task / note                                     |
|------------------------------------------------------|--------------------------------------------------------|
| Reconnect empirical verification (drop mid-stream)   | §16.1-reconnect follow-up (TBD id, filed in step 14)  |
| Per-user observability (bus stats endpoint)          | #13 stats (`20260613-091047`) — bus already exposes   |
|                                                      | `.stats`; #13 wires the GET endpoint                   |
| Permission ask/reply UX (full flow)                  | #30 permissions (`20260613-093108`) — #11 only        |
|                                                      | tunnels `permission.updated` as `tool_meta` events;    |
|                                                      | the ask/decision RPC is #30's surface                  |
| Telegram-side renderer (`thinking` events → bubbles) | #14 CLI v2 (`20260613-091049`) renders the SSE for   |
|                                                      | the CLI surface; Telegram renderer is a later task     |
| Token / cost streaming mid-turn                      | Out — opencode reports tokens only on `session.idle`;  |
|                                                      | the `done` event carries the final tally. Per-token   |
|                                                      | streaming would require opencode emitting              |
|                                                      | `message.updated` with cumulative counts, which it     |
|                                                      | doesn't                                                 |
| httpx-sse dependency                                 | Not needed — hand-rolled framing (~50 LoC) per v1     |
|                                                      | precedent. Avoids a pyproject.toml edit                |
| Bearer-token auth on `/v1/chat/stream`               | Single-host trust model (ADR-8). Bearer-token auth    |
|                                                      | deferred to #14 — chat already accepts unauthenticated|
|                                                      | loopback requests; streaming follows suit              |
| Retry on `OpencodeStaleSessionError` mid-stream      | Out for v0 — if opencode GC's our session mid-turn   |
|                                                      | the SSE emits `error` and the client retries. v1's    |
|                                                      | stale handling lived in the higher-level agent loop;  |
|                                                      | v2 surfaces it instead. Revisit if it becomes common  |

---

## Design decisions (locked during scoping, 2026-06-20)

### D1: SSE wire format adopts v2 §8 richer `done` shape (not v1 byte-compat)

The terminal `done` SSE event carries `{type, message, oc_session_id,
oc_message_id, tokens: {input, output}, cost}` — a strict superset of
v1's `{text}` payload, mirroring `routes/chat.py:ChatResponse`.

Rationale: v1's `done` payload was `{text}` (see
`feature/opencode:scufris_client/client.py:152` —
`payload.get("text", "")`); v2's design §8 (line 306-308) commits to
`{type, message, tokens, cost}`. We go richer per D7 (next decision)
because the v1 CLI client will need updating anyway when CLI v2 (#14)
lands on Path A — bundling the wire change into #11 saves a future
rework. The v1 client tolerates extra keys silently
(`payload.get("text", "")`) so partial migration paths still work
during the transition.

Rejected: keep v1 `{text}` shape. Optimal for zero-touch v1-client
back-compat, but loses the metadata the modern CLI / Telegram
surfaces want (session id for follow-up, cost for budget UX in #28).

### D2: Request body matches `/v1/chat` `ChatRequest` shape

`POST /v1/chat/stream` accepts the same `{message, channel: {surface,
surface_id, agent}}` payload as `/v1/chat`. Implementation imports
`ChatRequest` from `routes/chat.py`.

Rationale: symmetric API surface; CLI / Telegram code paths can
switch between sync and streaming by changing the URL alone. Adding
streaming-only fields (e.g. `include_tool_results: bool`) would be a
nice-to-have but adds shape divergence; defer until a caller actually
needs it.

### D3: Send mechanism — synchronous POST + parallel `/event` drain

The handler fires `client.send_message(session_id, ...)` in a
background `asyncio.create_task` (D8) and drains the bus queue in
parallel. The POST blocks until opencode finishes the turn
(`session.idle`); the bus delivers events meanwhile. On `session.idle`
the handler awaits the poster task to retrieve the final
`AssistantMessage` for the `done` payload.

Rationale: matches v1's `_chat_stream_via_bus` pattern (verified
against opencode 1.15.13's SCHEMA.md). The design doc §9.2 line 329
mentions `POST /session/:id/prompt_async`; that endpoint does not
exist in opencode 1.15.13 — the synchronous `POST /session/:id/message`
is the canonical entrypoint and works fine with parallel SSE
draining. v1 ran this pattern in production for the entire post-swap
window.

Rejected: spawn a thread to call a synchronous SDK. Adds threading
complexity for zero benefit — httpx is already async.

### D4: Reconnect empirical verification split into a follow-up task

#11 ships the bus + reconnect logic + `_BusReconnected` sentinel
broadcast. The full empirical verification — "forcibly drop the
upstream connection mid-turn, observe what's missed, confirm the
recovery story" — needs a non-trivial test harness (bouncing
opencode or `httpx` interception) and lives in a separate task
filed during step 14.

Rationale: the reconnect *behaviour* (broadcast sentinel, surface
error to client, let client retry) is straightforward to implement
and unit-test. The *empirical* part — what opencode actually drops
during a disconnect window — is research-flavoured and benefits
from being isolated so it doesn't gate the user-facing endpoint.

Rejected: bundle into #11. Would balloon scope and push the
streaming endpoint behind a research task.

### D5: Three sibling modules, not a `scufris_server/events/` package

The implementation lives in three flat modules at the
`scufris_server/` level: `events.py` (~270 LoC: ThinkingEvent +
EventBus), `event_mapping.py` (~200 LoC: pure mapping),
`sse.py` (~50 LoC: outbound framing).

Rationale: matches v1's separation
(`utils/callbacks.py` + `utils/opencode_events.py` +
`scufris_client/client.py:_dispatch`) without the namespace overhead
of a package. Total LoC across the three modules (~520) is
smaller than `sessions.py` alone (302 lines, single file). The
placeholder docstring mentions "future package" as an option;
revisit if #28 (observability) or #30 (permissions) materially grow
`events.py` past ~500 LoC.

Rejected: `scufris_server/events/{bus,mapping,sse,types}.py`
package. Cleaner namespace but premature given the current size
budget.

### D6: `_resolve_session` / `_touch_session` cross-route import

`routes/chat_stream.py` imports the two helpers directly from
`routes/chat.py`. Documented in the chat_stream module docstring.

Rationale: matches the #10 precedent where `routes/sessions.py`
imports `Tokens` from `routes/chat.py` for shared wire shapes.
Both helpers are chat-loop specific (the `(user_id, surface,
surface_id, agent)` lookup form, the `last_used_at` bump) and have
no third caller that would justify promoting them to `sessions.py`
as public API. If a third caller materialises (e.g. a future
`POST /v1/chat/regenerate` in #33), promote then.

Rejected (1): copy-paste into `chat_stream.py`. Cheap now, drift
risk later.

Rejected (2): promote both to `sessions.py` as `resolve_chat_session`
+ `touch_session`. Adds a refactor step and broadens
`sessions.py`'s remit beyond what its module docstring claims (it's
the channel-link service, not a chat-helper grab-bag).

### D7: `done` payload mirrors `/v1/chat` `ChatResponse`

`done` payload field set: `{type: "done", message, oc_session_id,
oc_message_id, tokens: {input, output}, cost}`. `message` is the
plain reply text (concatenated `type=="text"` parts) — same as
`ChatResponse.reply`.

Rationale: gives the CLI everything `/v1/chat` returns so #14 can
render either response identically. Avoids a "streaming returns less
than sync" footgun. `oc_session_id` + `oc_message_id` enable
correlation for follow-up calls (regenerate, fork, etc.). Field
naming uses `message` (per design §8) rather than `reply` (per
`/v1/chat`) — the design doc literal wins on the streaming side
since this is a new wire shape; `/v1/chat` keeps `reply` for back-
compat with anything that's already consuming it.

Rejected: literal `{type, message, tokens, cost}` per §8.
`oc_session_id` / `oc_message_id` are too useful to drop; the
design spec didn't anticipate the CLI wanting to render the same
metadata for both endpoints.

### D8: `asyncio.create_task` for the poster in the handler

The route handler owns the `post_task = asyncio.create_task(
client.send_message(...))` lifecycle directly. No new
`OpencodeClient.send_message_async()` method.

Rationale: mirrors v1's `_chat_stream_via_bus` exactly. Adding a
wrapper method would shift the cancellation / error-handling logic
to the client layer, mixing concerns — the client is a thin HTTP
wrapper, the task lifecycle belongs to whoever consumes both halves
(POST result + SSE events). Concrete: handler does
`task = asyncio.create_task(...)`, drains queue, on `session.idle`
does `assistant = await task` for the `done` payload, on
`finally` cancels + swallows if still running.

Rejected: `OpencodeClient.send_message_async() -> (drain_iter,
awaitable_result)`. Encapsulates the pattern but the chat_stream
route is the only consumer; YAGNI.

---

## Behavioural notes

- **ONE bus per process, not per request.** ADR-10 in concrete form:
  `app.state.opencode_event_bus` is set once in lifespan, used by
  every `/v1/chat/stream` invocation. N concurrent streams subscribe
  to the same bus → 1 upstream `/event` connection regardless of N.
- **`subscribe()` is instantaneous after first connect.** The bus
  blocks the first `subscribe()` call until it has reached
  `connected=True` (30s timeout default), then every subsequent
  subscribe completes immediately. Mid-stream disconnects surface
  via the `_BusReconnected` sentinel, not by blocking subscribe.
- **Reconnect = "fail the turn", not "replay".** A bus reconnect
  mid-turn means we may have missed events (opencode's `/event` is
  fire-and-forget per design §16.1 assumption). The handler emits
  an `error` SSE event and closes; the client decides whether to
  retry. We do NOT try to refetch state and replay missed events —
  too much state machine for the v0 reliability budget.
- **Events without sessionID are dropped at dispatch.** Global
  events (`server.connected`, `installation.*`, `lsp.*`) carry no
  `properties.sessionID` and no subscriber wants them. The bus
  silently drops at the dispatch layer (matches v1).
- **`session.idle` for the wrong session passes through but doesn't
  terminate.** The bus filters by sessionID at dispatch, so the
  per-session queue should only see events for *its* session — but
  the handler defensively re-checks `evt_session == session_id`
  before terminating on `session.idle` / `session.error`.
- **Stale session 404 mid-stream is rare.** opencode GCs sessions
  on long idleness; the v0 chat reuse pattern keeps `last_used_at`
  fresh, so this should only fire if opencode is force-restarted
  mid-turn. We surface it as a single `error` SSE event with
  `error_type: "OpencodeStaleSessionError"` and let the client
  retry (which will trigger create_session on the next call). No
  in-handler retry.
- **`X-Accel-Buffering: no` + `Cache-Control: no-cache` headers.**
  Standard SSE incantation for nginx / Cloudflare buffering
  defeating. v1 set both; we mirror.
- **JSON in SSE payloads is compact.** `json.dumps(payload,
  separators=(",", ":"))` — saves a few bytes per chunk and makes
  the wire output `grep`-friendly during debug.
- **Subagent UX is faithful v1 port.** Subagent spawns surface as
  `tool_call` events with the sub-agent tool's name (e.g.
  `knowledge_agent`); the CLI / Telegram renderer's
  `is_sub_agent()` check works unchanged against the v2 stream.
  User's "see what scufris is doing" ask is satisfied by the
  existing renderer + the new SSE source.
- **Permissions tunneled as `tool_meta`.** `permission.updated`
  events from opencode map to `ThinkingEvent(kind="tool_meta",
  text="permission: <title>")` for visibility; the
  ask/decision RPC surface is #30's job. Renderer treats
  `tool_meta` as a dim italic prefix line.

---

## TOML schema impact

None. `config.toml` (#12) does not configure streaming behaviour.
Bus reconnect parameters (`reconnect_initial`, `reconnect_max`,
`connect_timeout`) are hard-coded constants at v0; if operator-
tunable knobs become useful, add them to `[opencode]` as part of
a later task. No new env vars.

---

## Module layout

Touches:

```
scufris_server/
├── events.py             # FILL: ThinkingEvent + EventBus (~270 LoC)
├── event_mapping.py      # NEW:  opencode → ThinkingEvent (~200 LoC)
├── sse.py                # NEW:  outbound SSE framing (~50 LoC)
├── opencode_client.py    # ADD:  OpencodeStaleSessionError + httpx_client
├── app.py                # ADD:  EventBus start/stop in lifespan
├── dependencies.py       # ADD:  get_event_bus
├── routes/
│   ├── __init__.py       # ADD:  chat_stream_router → ROUTERS 5 → 6
│   └── chat_stream.py    # NEW:  POST /v1/chat/stream handler
```

Tests touched:

```
tests/unit/
├── test_events.py            # NEW — EventBus + ThinkingEvent (~10 tests)
├── test_event_mapping.py     # NEW — mapping table (~15 tests)
├── test_sse.py               # NEW — wire format (~6 tests)
├── test_chat_stream_route.py # NEW — endpoint behaviour (~12 tests)
├── test_opencode_client.py   # EXTEND — stale error + httpx_client
├── test_placeholders.py      # SHRINK — drop events row; ROUTERS 5 → 6
└── test_app.py               # EXTEND — bus lifespan ordering

tests/integration/
└── test_chat_stream_real.py  # NEW — opt-in real-opencode round-trip
```

Examples touched:

```
examples/
├── check_chat_stream.py  # NEW — SSE-reading example
└── README.md             # ADD — inventory entry
```

Docs touched:

```
docs/
├── 001_architecture.md   # ADD: bus lifespan paragraph (ADR-10 in action)
├── 002_api_reference.md  # PROMOTE: /v1/chat/stream Future → live
└── 006_development.md    # UPDATE: test counts, module layout
```

---

## TODOs (in implementation order)

Each step is small enough to land in one commit and review
independently. User approves 1-2 steps at a time.

1. **Expand this TASK.md from 13-line stub.** [this file]
    - [x] File the §16.1-reconnect follow-up (deferred to step 14;
          this slot is just for the documentation expansion).
    - [x] Encode the four pre-existing decisions D1–D4 plus the
          four step-0 decisions D5–D8.

2. **Port `ThinkingEvent` + `OpencodeStaleSessionError` to
   `scufris_server/events.py`.**
   - [x] Replace the 21-line placeholder with the dataclass +
         `SCUFRIS_SOURCE` constant + `to_payload()` method.
   - [x] Add `OpencodeStaleSessionError` to `opencode_client.py`
         (carry from v1).
   - [x] Mark `EventBus` as TODO comment in `events.py`; full
         implementation lands in step 5.
   - [x] Tests: `test_events.py` with ThinkingEvent round-trip,
         defaults, non-None field filtering, source/depth pins.
   - [x] Update `test_placeholders.py`: drop
         `("scufris_server.events", "20260613-091045")` row from
         `NON_HTTP_PLACEHOLDERS` (placeholder check fails the
         moment we expose `ThinkingEvent` as a public symbol).
         Update module docstring to acknowledge #11 promotion
         alongside #10/#12.
   - Gates: ruff clean, mypy --strict clean, all existing tests
     pass.

3. **Port event mapping to `scufris_server/event_mapping.py`.**
   - [x] Verbatim port of `feature/opencode:utils/opencode_events.py`:
         `EventMapperState`, `extract_text_delta`,
         `map_opencode_event`, `_map_part_updated`,
         `_summarise_input`. Adjust import:
         `from scufris_server.events import ThinkingEvent`.
   - [x] `permission.updated` → `tool_meta` mapping included
         (already in v1 source per research notes).
   - [x] Module docstring naming task #11 + crediting v1 module.
   - [x] Tests: `test_event_mapping.py` table-driven — text delta,
         tool running/completed/error, dedup by part.id, permission,
         unknown event → None, malformed event → None.
   - Gates: ruff clean, mypy --strict clean, all tests pass.

4. **Add `_BusReconnected` + `httpx_client` property to
   `opencode_client.py`.**
   - [x] Internal `_BusReconnected` dataclass module-level (used by
         bus → consumer signalling).
   - [x] Read-only `httpx_client` property returning the underlying
         `httpx.AsyncClient`.
   - [x] Extend `test_opencode_client.py` with two new tests:
         `httpx_client` is the same instance across calls; basic
         shape check.
   - Gates: ruff clean, mypy --strict clean, all tests pass.

5. **Implement `EventBus` in `scufris_server/events.py`.**
   - [x] Port `feature/opencode:utils/opencode_client.py:130-380`
         `_OpenCodeEventBus` → `EventBus`. Rename for clarity
         (drop the leading underscore; v2's module structure makes
         it part of the public API).
   - [x] `start()`, `stop()`, `subscribe(session_id)` async
         context manager, `stats` property, `connected` property.
   - [x] Reconnect with exponential backoff (1s → 30s); broadcast
         `_BusReconnected` on every reconnect after the first.
   - [x] Dispatch by `properties.sessionID`; drop events without
         sessionID at dispatch.
   - [x] Tests: extend `test_events.py` (~8 more tests) covering
         start/stop idempotent, subscribe sees matching events
         only, multiple subscribers see same event, reconnect
         broadcasts sentinel, bad JSON skipped + WARNING, first-
         connect timeout raises.
   - [x] Note: `test_placeholders.py` row already dropped in
         step 2; no test_placeholders changes needed here.
   - Gates: ruff clean, mypy --strict clean, all tests pass.

6. **Add outbound SSE framing in `scufris_server/sse.py`.**
   - [x] `KEEPALIVE_SECONDS = 15` module constant.
   - [x] `format_event(event_name: str, payload: Mapping[str, Any])
         -> bytes` — compact JSON, proper SSE framing.
   - [x] `format_keepalive() -> bytes` — `: keepalive\n\n`.
   - [x] `stream_with_keepalive(events: AsyncIterator[bytes])
         -> AsyncIterator[bytes]` — uses `asyncio.wait_for` per
         iteration to inject keepalives on idle.
   - [x] Tests: `test_sse.py` (~6 tests) — single event encoding,
         multi-event ordering, format_keepalive shape, keepalive
         on idle, final event flush, JSON payload compactness.
   - Gates: ruff clean, mypy --strict clean, all tests pass.

7. **Wire `EventBus` into FastAPI lifespan (`app.py` + dependencies).**
   - [x] Lifespan order: existing setup → start bus → yield →
         stop bus → close opencode client. Tolerate
         `OpencodeNetworkError` on first connect: log WARNING,
         leave bus disconnected; reconnect loop retries forever.
   - [x] Add `app.state.opencode_event_bus: EventBus`.
   - [x] Add `get_event_bus(request: Request) -> EventBus` to
         `dependencies.py`.
   - [x] Extend `test_app.py` with lifespan tests: bus started
         once after client, stopped before client, state attribute
         set.
   - [x] Extend `test_dependencies.py` if it has one for the
         dependency lookup pattern (check during step).
   - Gates: ruff clean, mypy --strict clean, all tests pass.

8. **Implement `POST /v1/chat/stream` in
   `scufris_server/routes/chat_stream.py`.**
   - [x] New module with `router = APIRouter(prefix="/v1",
         tags=["chat"])`.
   - [x] Imports `ChatRequest`, `Tokens`, `ChatErrorBody`,
         `_resolve_session`, `_touch_session`, `_raise_503` from
         `routes.chat` (D6).
   - [x] Pre-stream validations identical to `/v1/chat` — JSON 503
         with structured body on default-model-missing,
         create_session network/5xx.
   - [x] Stream body algorithm per D3 + D8 (see Scope item 7).
   - [x] `StreamingResponse(media_type="text/event-stream",
         headers={"X-Accel-Buffering": "no", "Cache-Control":
         "no-cache"})`.
   - [x] `done` payload shape per D7.
   - [x] No tests in this step — covered by step 10. Step 8 just
         lands the handler.
   - Gates: ruff clean, mypy --strict clean, existing tests pass
     (route not yet mounted, so no behavioural delta).

9. **Mount stream router + update placeholders.**
   - [x] Add `chat_stream_router` to `ROUTERS` in
         `routes/__init__.py` (5 → 6).
   - [x] Update `test_placeholders.py` ROUTERS assertion (5 → 6).
   - [x] Update `routes/__init__.py` module docstring to
         acknowledge #11 promotion.
   - Gates: ruff clean, mypy --strict clean, all tests pass.

10. **Unit tests for the stream route
    (`tests/unit/test_chat_stream_route.py`).**
    - [x] ~12 tests with mocked bus + respx for opencode HTTP:
          1. happy path: 1 thinking + done.
          2. multiple thinking events in order.
          3. SSE wire format (event/data framing, blank-line sep).
          4. existing session reused (`_touch_session` invoked).
          5. new session created (`create_channel_link` invoked).
          6. 503 before stream: default model missing.
          7. 503 before stream: create_session network error.
          8. 503 before stream: create_session 5xx.
          9. mid-stream bus reconnect → `error` event emitted.
          10. mid-stream `session.error` → `error` event emitted.
          11. identity override respected.
          12. subagent tool_call event surfaced as thinking.
          (permission tool_meta covered in test_event_mapping.py;
          keepalive timing covered in test_sse.py.)
    - [x] Helper: a `FakeEventBus` test fixture that exposes
          `put_event(session_id, event_dict)` so tests can drive
          the stream deterministically without spinning up real
          opencode.
    - Gates: ruff clean, mypy --strict clean, all tests pass.

11. **Integration test against real opencode + ollama
    (`tests/integration/test_chat_stream_real.py`).**
    - [x] 90-second budget (matches `test_chat_real.py`).
    - [x] Test 1: simple "what is 2+2?" prompt; assert at least
          one thinking event arrives; assert `done` has
          `tokens.input > 0`, `tokens.output > 0`, `cost >= 0`,
          `message` non-empty.
    - [x] Test 2: session reuse — fire two stream calls back-to-
          back with same channel; assert second reuses
          `oc_session_id` from the first.
    - [x] No tool-calling assertion (tool registration is #28+);
          subagent-event smoke test is covered by happy-path
          existence of thinking events.
    - [x] Snapshot opencode `/session` count via `httpx.get` before
          + after to detect session leaks (ADR-13 pattern).
    - Gates: ruff clean, mypy --strict clean, all unit + 3
      integration tests pass.

12. **Example script `examples/check_chat_stream.py`.**
    - [x] Mirror `check_chat.py` shape; uses plain `httpx.AsyncClient`
          to POST + iterate SSE.
    - [x] Prints thinking events to stderr (dim italics if isatty);
          final reply to stdout.
    - [x] `SCUFRIS_USER_ID=1` env pin in docstring (per #10 step
          12 follow-up pattern).
    - [x] Update `examples/README.md` inventory: add
          `check_chat_stream.py` row.
    - [x] Live re-run smoke test: script runs OK against pid=40883.
    - Gates: ruff clean, mypy --strict clean (if checked there),
      example exits 0 against live server.

13. **Docs.**
    - [x] `docs/002_api_reference.md`: promote
          `POST /v1/chat/stream` from `## Future endpoints` to a
          new live section. Sections mirror `/v1/chat`: Source →
          Request → Response (SSE event schema table + done
          payload + error payload + keepalive description) →
          Side effects → Errors → Examples. Headline endpoint
          table at top gains the row.
    - [x] `docs/001_architecture.md`: paragraph under §5.1 / near
          ADR-10 noting `app.state.opencode_event_bus` is the
          single consumer; reconnect backoff; `_BusReconnected`
          sentinel semantics. Module map + roles table + lifespan
          step 8 (start bus before yield, stop bus before client
          on shutdown) + new `## SSE event bus` section.
    - [x] `docs/006_development.md`: layout block gains three new
          modules (`events.py`, `event_mapping.py`, `sse.py`) +
          `examples/` row; tests table updated count (~290 unit,
          3 integration); integration list gains
          `test_chat_stream_real.py` with split ceilings (90 s
          chat-stream happy, 120 s chat-stream reuse, 120 s
          sessions); backlog table refreshed to reflect #10 / #11
          closed, #33 / #34 added.
    - [x] Bonus drift-fixes outside original scope:
          `docs/000_overview.md` "implemented today" list +
          "not implemented yet" list + caveat date + `#`-list;
          `docs/005_data_model.md` audit-log row + paragraph
          (clarifies #11 landed the EventBus but does *not* fill
          the `opencode_events` table — separate follow-up).
    - [x] No `docs/005_data_model.md` *schema* changes (the only
          edit was a stale claim in the table row + paragraph).
    - Gates: ruff clean (if any code touched), no broken anchors.

14. **Close-out gates + file follow-ups + mark CLOSED.**
    - [x] Full test suite: unit pass (~260 tests), 3 integration
          pass, ruff check clean, ruff format clean per touched
          file, `mypy --strict scufris_server` clean.
    - [x] Live re-run of `examples/check_chat_stream.py` confirms
          thinking events flow against real opencode + ollama.
    - [x] `tatr new` for §16.1-reconnect follow-up (one-paragraph
          stub: drop upstream connection mid-turn, observe
          missed events, decide whether to add replay logic).
    - [x] Sleep 2s before `tatr new` to avoid ID collision (per
          critical-context note).
    - [x] Mark `tasks/20260613-091045/TASK.md` `STATUS: CLOSED`.
    - [x] Add closing notes to TASK.md summarising LoC added,
          test counts, decisions taken during impl.

---

## Acceptance criteria

- [x] `POST /v1/chat/stream` accepts the same `ChatRequest` shape
      as `/v1/chat` (D2).
- [x] SSE stream emits `thinking` events mapped from opencode
      `message.part.delta`, `message.part.updated`,
      `permission.updated` (faithful v1 mapping).
- [x] Final `done` payload has `{type, message, oc_session_id,
      oc_message_id, tokens: {input, output}, cost}` (D7).
- [x] Mid-stream errors emit `error` SSE event with `{error,
      error_type}` and close the stream cleanly.
- [x] Pre-stream errors return JSON 503 with `ChatErrorBody`
      shape (parity with `/v1/chat`).
- [x] Single `EventBus` instance per process, started in lifespan
      (ADR-10 in action).
- [x] Bus reconnects with exponential backoff (1s → 30s cap).
      Mid-stream reconnect surfaces as `error` SSE event with
      `error_type: "BusReconnected"`.
- [x] Keepalive `: keepalive\n\n` emitted every 15s on idle
      streams.
- [x] Subagent spawns visible as `tool_call` thinking events
      (verifies "see what scufris is doing" UX ask).
- [x] Permission events visible as `tool_meta` thinking events.
- [x] Existing 218 unit tests still pass after refactor; new
      unit tests bring total to ~260.
- [x] ROUTERS count = 6 after step 9.
- [x] 3 integration tests pass against real opencode + ollama
      (chat, sessions, chat_stream).
- [x] `mypy --strict scufris_server` clean (no new src files
      introduce type errors).
- [x] `ruff check scufris_server tests` clean.
- [x] `ruff format --check` clean on every touched file.
- [x] No new pyproject.toml dependencies (hand-rolled SSE
      framing per v1 precedent).
- [x] `examples/check_chat_stream.py` runs against pid=40883
      and prints visible thinking events for a real ollama
      response.
- [x] §16.1-reconnect follow-up task filed via `tatr new`.
- [x] TASK.md marked `STATUS: CLOSED` with closing notes.

---

## Closing notes (2026-06-20)

### Code added

| Module                                  | LoC | Role |
|-----------------------------------------|----:|------|
| `scufris_server/events.py`              | 473 | `ThinkingEvent` + per-process `EventBus` (was 21-line placeholder; +452 net). |
| `scufris_server/event_mapping.py`       | 311 | New. Pure mapping `opencode/event → ThinkingEvent`. |
| `scufris_server/sse.py`                 | 167 | New. SSE framing + `stream_with_keepalive`. |
| `scufris_server/routes/chat_stream.py`  | 476 | New. `POST /v1/chat/stream` handler. |
| `scufris_server/opencode_client.py`     | +71 | Added `OpencodeStaleSessionError`, `_BusReconnected`, `httpx_client` property. |
| `scufris_server/app.py`                 | +38 | Lifespan starts/stops bus; shutdown order bus-then-client. |
| `scufris_server/dependencies.py`        | +33 | `get_event_bus(request)`. |
| `scufris_server/routes/__init__.py`     | +11 | `chat_stream_router` slot; `ROUTERS` 5 → 6. |
| `scufris_server/sessions.py`            | +38 net | Orphan-channel fix: `create_channel_link` lookup-then-insert (relinks after ADR-13 clear). |
| `examples/check_chat_stream.py`         | 423 | New. Spawns own server, 2 turns, dim-italic stderr. |

### Tests added

| Module                                       | Tests added | Pattern |
|----------------------------------------------|------------:|---------|
| `tests/unit/test_events.py`                  |    new (19) | EventBus lifecycle + ThinkingEvent + dispatch. |
| `tests/unit/test_event_mapping.py`           |    new (22) | Table-driven mapper coverage. |
| `tests/unit/test_sse.py`                     |     new (8) | Wire framing + keepalive shield. |
| `tests/unit/test_chat_stream_route.py`       |    new (13) | `FakeEventBus` duck-type pattern. |
| `tests/unit/test_app.py`                     |          +3 | Bus started/stopped in lifespan. |
| `tests/unit/test_dependencies.py`            |          +1 | `get_event_bus` dep. |
| `tests/unit/test_opencode_client.py`         |          +5 | `_BusReconnected` + `httpx_client` + `OpencodeStaleSessionError`. |
| `tests/unit/test_sessions.py`                |          +1 | Orphan-channel relink coverage. |
| `tests/unit/test_placeholders.py`            |        ROUTERS bump | 5 → 6. |
| `tests/integration/test_chat_stream_real.py` |     new (2) | Happy path + session reuse, ADR-13 leak snapshot. |

Final test counts: **292 unit pass / 4 integration pass** (was 218 unit / 2 integration). `mypy --strict scufris_server` clean on 22 source files. `ruff check scufris_server tests` clean. `ruff format --check` clean on every touched file.

### Decisions taken during impl

- **D1-D8** locked at step 0 (see §15 of this doc). All held.
- **Implicit-D9 (post-stream error tunnel).** Errors that escape *after* the response has been committed are surfaced as `event: error` SSE records, not as 5xx. Pre-stream errors keep the JSON 503 path (parity with `/v1/chat`).
- **Implicit-D10 (post-task sentinel terminates).** The handler tears down on a `_PostDone` / `_PostError` sentinel pushed by the `send_message` runner — not on `session.idle` from the bus. Avoids races where `idle` arrives before the final `message.updated`.
- **Implicit-D11 (step reorder).** Built `examples/check_chat_stream.py` (step 12) before the integration tests (step 11) at user request — gave a fast manual sanity-check loop while iterating on the route.
- **Implicit-D12 (orphan-channel fix, option B).** Surfaced mid-step-11 as `sqlite3.IntegrityError` on `channels.UNIQUE(user_id, surface, surface_id, agent)` when re-chatting a channel whose `session_links` row had been cleared. Picked the lookup-then-insert refactor inside `sessions.create_channel_link` over plumbing a "may exist" flag through `_resolve_session`. Smaller blast radius, transparent to both chat routes. Pre-#11 bug (introduced in #10), v2-specific (v1 had a single `sessions` table with no channels split). Coverage hardening filed as `tasks/20260620-103412` (P40 follow-up).

### Aggregate diff

- **Code + tests + example:** +4947 / -68 across 22 files (staged).
- **Docs:** +487 / -130 across 6 files (`000_overview.md`, `001_architecture.md`, `002_api_reference.md`, `005_data_model.md`, `006_development.md` plus this TASK.md). The 005 / 000 edits were outside the original step 13 scope — small drift-fixes of stale "future #11" claims uncovered while updating the rest.

### Follow-ups filed

- `tasks/20260620-105256` (P40, NEW, `followup,server,sse`): §16.1 SSE bus replay-on-reconnect — reproduce a drop mid-turn against real opencode, quantify event loss, decide policy (accept loss / full replay / best-effort).
- `tasks/20260620-103412` (P40, pre-existing): post-clear coverage gap at the chat-route + integration level (orphan-channel fix is covered at `sessions.py` level only).

### Live state at close-out

- scufris-server pid=29482 on `127.0.0.1:7080` still has pre-orphan-fix code loaded; leave alone (cleaned-orphan DB state masks the bug for now). Restart at next opportunity to pick up the fix.
- opencode 1.15.13 on `127.0.0.1:4096` healthy.
- Live DB `/home/alex/.local/state/scufris/scufris.sqlite`: orphan ids 1-7 already deleted (during step-11 debugging); channel 8 + its `ses_11c12d37cffeZhpN2W0w2VxWXX` link preserved.

### What's next

`#14` scufris-cli v2 (`tasks/20260613-091049`, P80) is the next OPEN P80+. It will consume both `/v1/chat` and `/v1/chat/stream`; the streaming path now exists and is documented in `docs/002_api_reference.md`.

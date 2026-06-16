# 002 — API reference

Every endpoint scufris-server exposes today, with a field-by-field
explanation of every request and response.

The headline:

| Method | Path                              | Purpose                                            | Status |
|--------|-----------------------------------|----------------------------------------------------|--------|
| GET    | `/v1/healthz`                     | Liveness probe + opencode reachability             | live   |
| GET    | `/v1/version`                     | scufris version + cached opencode version          | live   |
| POST   | `/v1/identity/resolve`            | Resolve `(surface, surface_id)` → user             | live   |
| POST   | `/v1/chat`                        | Synchronous single-turn chat                       | live   |
| GET    | `/v1/sessions`                    | List a user's channels with enriched session info  | live   |
| POST   | `/v1/sessions/{channel_id}/clear` | Drop the `session_links` row for one channel       | live   |
| POST   | `/v1/clear`                       | Drop every `session_links` row for one user        | live   |
| —      | `/v1/stats`                       | Per-user telemetry                                 | placeholder (#13) |
| —      | `/v1/permissions/*`               | Permission reply for opencode tool calls           | placeholder (#30) |
| —      | `/v1/chat/stream`                 | SSE streaming chat                                 | not yet (#11)     |
| —      | `/v1/sessions/{channel_id}/fork`  | Fork a channel onto a new opencode session         | not yet (#33)     |

Placeholders 404 today — the routers exist as empty `APIRouter`
instances and aren't even mounted. They're imported by
`routes/__init__.py` so any import-time breakage shows up immediately
when the server boots, but they don't add routes to the app.

## Conventions across all endpoints

- All paths are under `/v1/`.
- Request bodies are JSON; `Content-Type: application/json`.
- Response bodies are JSON.
- Validation errors → HTTP 422 with FastAPI's standard `{detail: [...]}`
  body (this is pydantic's territory; we don't customise it).
- Loopback-only by default. Bearer-token auth lands in `#14`.
- Every response carries an `X-Request-Id` header. It's a 26-char
  ULID minted by `RequestIdMiddleware` (`logging.py:217`). If the
  client sends one and it's a valid ULID, the server echoes it
  through; otherwise it's replaced with a fresh one. Use it for log
  correlation: every server-side log line for the request includes
  the same `request_id` field.

### Error response shape

The chat endpoint (and any future endpoint that hits opencode) uses
this shape for service-unavailable failures:

```json
{
  "error": "<human-readable message>",
  "error_type": "<symbolic enum value>"
}
```

Wrapped in FastAPI's `HTTPException(status_code=503, detail=...)`,
which surfaces as `{"detail": {"error": ..., "error_type": ...}}` on
the wire. `error_type` values are documented per endpoint.

422 (validation) and 500 (uncaught exception) use FastAPI's defaults
and are not customised today.

## `GET /v1/healthz`

Source: `routes/health.py:76`.

Always returns 200. The body reports the *current* state of opencode;
`ok=true` means opencode is reachable right now, not that scufris is
alive (if you got a 200, scufris is alive by definition).

Re-probes opencode on every call with a 5-second budget. The
upstream's own client default is 30s, but a load balancer hitting
`/v1/healthz` won't tolerate that — health endpoints have to fail
fast.

### Request

No body. No query parameters.

### Response

Two shapes, distinguished by `opencode`:

**Healthy:**
```json
{
  "ok": true,
  "opencode": {
    "healthy": true,
    "version": "1.15.13"
  }
}
```

**Degraded:**
```json
{
  "ok": false,
  "opencode": {
    "error": "cannot reach opencode at http://127.0.0.1:4096: ConnectError(...)"
  }
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `ok` | `bool` | True iff opencode responded with HTTP 200 to `/global/health` within the 5s budget. |
| `opencode.healthy` | `bool` | The `healthy` flag from opencode's own health response. We pass it through. |
| `opencode.version` | `string` | Opencode's reported version string (e.g. `"1.15.13"`). Cached on `app.state.opencode_version` and reused by `/v1/version`. |
| `opencode.error` | `string` | Present only on the degraded shape. Human-readable description of why the probe failed. Either a transport error message (`"cannot reach ..."`) or a non-200-status message (`"opencode /global/health returned HTTP 500"`). |

### Side effects

On a successful probe, refreshes `app.state.opencode_version` so the
next `/v1/version` cache-hits.

### Examples

```bash
curl -s http://127.0.0.1:7080/v1/healthz | jq
```

## `GET /v1/version`

Source: `routes/health.py:106`.

Returns scufris's own version plus the cached opencode version.
Cheap by default — answers from cache if populated. Falls back to a
live probe of opencode (5s budget) on cache miss.

### Request

No body. No query parameters.

### Response

```json
{
  "version": "0.1.0",
  "opencode_version": "1.15.13"
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `version` | `string` | scufris-server's own version, from `scufris_server.__version__`. Bumped manually per release. |
| `opencode_version` | `string \| null` | Opencode's reported version. `null` only if the boot probe failed *and* the lazy probe on this call also failed. The cache is populated at boot (lifespan step 6) and refreshed by every successful `/v1/healthz`. |

### Cache behaviour

- Cache hit (steady state): no opencode contact, returns immediately.
- Cache miss: lazy probe of opencode `/global/health`, 5s timeout.
  - Success → cache populated, returned in this response.
  - Failure → cache stays empty, `opencode_version: null`, next call
    retries.

Cache is stored on `app.state.opencode_version`. It's
process-scoped — restarts re-probe.

## `POST /v1/identity/resolve`

Source: `routes/identity.py:61`.

Resolve a `(surface, surface_id)` pair to a `users.id`. Same algorithm
as the implicit resolution that `/v1/chat` does internally, exposed as
a standalone endpoint so clients can show "signed in as alex" before
sending their first message.

See [`003_identity_and_users.md`](003_identity_and_users.md) for the
four-step resolver in full.

### Request

```json
{
  "surface": "cli",
  "surface_id": "alex"
}
```

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `surface` | `string` | yes (non-empty) | The user-facing channel: `"cli"`, `"telegram"`, `"web"`, etc. Free-form — no enum constraint, so future surfaces don't require a code change. |
| `surface_id` | `string` | yes (non-empty) | The per-surface identifier: a terminal user, a Telegram chat_id, a web tab id, … Whatever uniquely names "this user on this surface". |

Empty strings → 422. The validation lives in `ResolveRequest` (`routes/identity.py:41`).

### Response

```json
{
  "user_id": 2,
  "username": "alex",
  "surface": "cli",
  "surface_id": "alex",
  "bound_surfaces": [
    {"surface": "cli",      "surface_id": "alex"},
    {"surface": "telegram", "surface_id": "8231376426"}
  ]
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `user_id` | `int` | The `users.id` this `(surface, surface_id)` pair maps to. `1` is the seeded "default" user (everyone falls back to id=1 if not found via TOML). Higher ids are users introduced by `config.toml`. |
| `username` | `string` | The `users.username` for `user_id`. `"default"` for id=1; otherwise whatever was in `[user] username = "..."` in `config.toml`. |
| `surface` | `string` | Echo of the request surface. |
| `surface_id` | `string` | Echo of the request surface_id. |
| `bound_surfaces` | `list[BoundSurface]` | Every `surface_bindings` row currently associated with `user_id`, sorted `(surface ASC, surface_id ASC)`. Includes the binding that this call may have just materialised. Useful for "which other channels are this user on?". |

#### `BoundSurface`

```json
{"surface": "cli", "surface_id": "alex"}
```

| Field | Type | Meaning |
|-------|------|---------|
| `surface` | `string` | The surface name from the `surface_bindings` row. |
| `surface_id` | `string` | The surface_id from that row. |

### Side effects

- May insert into `users` (TOML hit for a previously-unseen username).
- May insert into `surface_bindings` (TOML hit OR default fallback —
  every miss writes a sticky binding row so subsequent calls
  cache-hit).
- Does *not* insert anything when `SCUFRIS_USER_ID` override is
  active — bindings stay clean for when you remove the override.

### Errors

- **422** — empty `surface` or `surface_id`.
- **500** — only on a misconfigured deployment: a stale
  `SCUFRIS_USER_ID` (validated at boot, but the table could be
  manually mutated) or a missing default-user row. The handler
  doesn't catch these — they're operator-visible bugs.

### Examples

```bash
# First call materialises the binding.
curl -s -X POST http://127.0.0.1:7080/v1/identity/resolve \
  -H 'content-type: application/json' \
  -d '{"surface":"cli","surface_id":"alex"}' | jq
# {"user_id":2,"username":"alex","surface":"cli","surface_id":"alex",
#  "bound_surfaces":[{"surface":"cli","surface_id":"alex"}]}

# Second call cache-hits — no DB writes, identical response.
curl -s -X POST http://127.0.0.1:7080/v1/identity/resolve \
  -H 'content-type: application/json' \
  -d '{"surface":"cli","surface_id":"alex"}' | jq
# (same response)

# Unknown surface_id → falls through to default user.
curl -s -X POST http://127.0.0.1:7080/v1/identity/resolve \
  -H 'content-type: application/json' \
  -d '{"surface":"cli","surface_id":"stranger"}' | jq
# {"user_id":1,"username":"default","surface":"cli","surface_id":"stranger",
#  "bound_surfaces":[{"surface":"cli","surface_id":"stranger"}]}
```

## `POST /v1/chat`

Source: `routes/chat.py:186`.

Synchronous single-turn chat. The server resolves identity, finds (or
creates) an opencode session for `(user_id, channel)`, sends the
message, blocks until opencode finishes the turn, and returns the
assistant's reply with token / cost metadata.

This is the only path today. Streaming (`/v1/chat/stream`) is `#11`.

### Request

```json
{
  "message": "reply with the single word: pong",
  "channel": {
    "surface": "cli",
    "surface_id": "alex",
    "agent": "build"
  }
}
```

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `message` | `string` | yes (non-empty) | The user's prompt text. Sent as a single `{"type":"text", "text": ...}` part to opencode. Multi-part input (images, attachments) is not supported by this endpoint today. |
| `channel` | `Channel` | yes | The conversational context this message belongs to. See below. |

#### `Channel`

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `surface` | `string` | yes (non-empty) | The user-facing channel: `"cli"`, `"telegram"`, `"web"`, etc. Same vocabulary as `/v1/identity/resolve`. |
| `surface_id` | `string` | yes (non-empty) | The per-surface user identifier. Routed through `resolve_user()` to find the `users.id` for this request. |
| `agent` | `string` | yes (non-empty) | Which scufris agent persona to use for this turn (e.g. `"build"`, `"plan"`). Forwarded to opencode at session-create time and on every `send_message`. Different agents under the same `(surface, surface_id)` get separate opencode sessions — agents don't share context. |

The `(user_id, surface, surface_id, agent)` tuple is the primary key
for opencode-session reuse. Same tuple → same opencode session →
shared context. Different tuple → fresh session.

### Response

```json
{
  "reply": "pong",
  "oc_session_id": "ses_abc123...",
  "oc_message_id": "msg_def456...",
  "tokens": {
    "input": 4096,
    "output": 268
  },
  "cost": 0.0
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `reply` | `string` | The concatenation of every `type=="text"` part in opencode's assistant response. Reasoning, step markers, and tool-call/result parts are dropped. Empty string is possible (e.g. a tool-only turn that produced no plain text). |
| `oc_session_id` | `string` | Opencode's session identifier (`ses_...`). Stable across turns within a channel — i.e. a follow-up message in the same channel returns the same `oc_session_id`. |
| `oc_message_id` | `string` | Opencode's identifier (`msg_...`) for the assistant message produced this turn. Different on every turn. |
| `tokens.input` | `int` | Input tokens consumed by the model this turn. `0` if opencode didn't report token counts (older models / providers may omit). |
| `tokens.output` | `int` | Output tokens produced this turn. Same caveat. |
| `cost` | `float` | The model's reported per-turn cost (in USD). `0.0` for self-hosted models like local ollama. |

The full `info` object opencode returns has more fields (`providerID`,
`modelID`, the full `tokens` shape with `reasoning` / `cache` / `total`)
but we don't expose them today. Adding them is a small change.

### Errors

| Status | error_type | Meaning |
|--------|------------|---------|
| 422    | (FastAPI default) | Malformed request body (empty fields, missing keys). |
| 503    | `DefaultModelMissing` | `app.state.opencode_default_model` is `None`. Either opencode was unreachable at boot (lifespan step 7 skipped), or no connected provider had a default model. Resolution: check `/v1/healthz`, fix the upstream, restart scufris. |
| 503    | `OpencodeNetworkError` | A `create_session` or `send_message` call hit a transport error (DNS failure, connection refused, read timeout). Body's `error` field carries the original `httpx` error description. |
| 503    | `OpencodeServerError` | Opencode returned a 5xx. Indicates opencode is broken, not us. Body's `error` carries opencode's response body. |
| 500    | (uncaught) | Either `OpencodeClientError` (4xx from opencode — bug in *our* request shape), a SQLite error, or some other unhandled exception. **Not** wrapped in our 503 envelope — these are operator-visible bugs we want raw. |

The 503 body matches the structured shape:

```json
{
  "detail": {
    "error": "cannot reach opencode at http://127.0.0.1:4096: ...",
    "error_type": "OpencodeNetworkError"
  }
}
```

### Side effects

- May insert into `users` and `surface_bindings` (same identity-
  resolution side effects as `/v1/identity/resolve`).
- May insert into `channels` and `session_links` (first call for a
  new `(user_id, channel)` triple).
- Updates `session_links.last_used_at` on every reuse.
- Sends one `POST /session` (only on first call per channel) and
  one `POST /session/:id/message` to opencode.

### Examples

First call against a new channel — creates the opencode session, may
take 15+ seconds on a cold model:

```bash
curl -s -X POST http://127.0.0.1:7080/v1/chat \
  -H 'content-type: application/json' \
  -d '{
        "message": "reply with the single word: pong",
        "channel": {
          "surface": "cli",
          "surface_id": "alex",
          "agent": "build"
        }
      }' | jq
```

Follow-up call to the same channel — reuses the session, much faster:

```bash
curl -s -X POST http://127.0.0.1:7080/v1/chat \
  -H 'content-type: application/json' \
  -d '{
        "message": "what was my last message?",
        "channel": {"surface":"cli", "surface_id":"alex", "agent":"build"}
      }' | jq
```

503 example (opencode down):

```json
{
  "detail": {
    "error": "POST /session: ConnectError('All connection attempts failed')",
    "error_type": "OpencodeNetworkError"
  }
}
```

## `GET /v1/sessions`

Source: `routes/sessions.py:232`.

List a user's channels with enriched session info pulled from
opencode. One row per channel, sorted by
`session_links.last_used_at` descending (most recently active first).

The endpoint INNER-JOINs `channels` × `session_links`, so channels
without a live link aren't returned today. ("List bound surfaces
that have never chatted" is a separate question deferred to a
future refinement; the immediate use case is "show me my live
sessions so I can clear stale ones.")

For each row, the handler asks opencode for the matching session
metadata and splices in `title`, `tokens`, and `cost`. When opencode
is unreachable (network error or 5xx), the same endpoint still
returns 200 with the scufris-side fields populated and the
opencode-supplied fields all `null`. A WARNING-level log record is
emitted with the underlying error. This is the "degraded GET"
contract (D1) — the channel list is critical enough to keep working
when opencode is down.

### Request

No body. Query parameter:

| Param | Type | Required | Meaning |
|-------|------|----------|---------|
| `user_id` | `int` | no | Caller-supplied principal. Ignored when `SCUFRIS_USER_ID` is set (override always wins, D2). Falls back to `1` (the seeded default user) if omitted and no override. |

### Response

```json
[
  {
    "channel_id": 12,
    "channel": {
      "surface": "cli",
      "surface_id": "alex",
      "agent": "build"
    },
    "oc_session_id": "ses_abc123...",
    "last_used_at": 1781609548,
    "title": "Pong reply",
    "tokens": {"input": 4096, "output": 213},
    "cost": 0.0
  }
]
```

| Field | Type | Meaning |
|-------|------|---------|
| `channel_id` | `int` | Scufris-side `channels.id`. The argument to `/v1/sessions/{channel_id}/clear` and (future, `#33`) `/fork`. |
| `channel` | `Channel` | The `(surface, surface_id, agent)` triple identifying this channel. Same shape as the `channel` field in `/v1/chat`. |
| `oc_session_id` | `string` | Opencode session id (`ses_...`) bound to this channel. |
| `last_used_at` | `int` | Unix seconds — last time this channel was touched by `/v1/chat`. The sort key. |
| `title` | `string \| null` | Opencode's title for the session. Auto-generated by opencode after the first turn. `null` on the degraded path. |
| `tokens` | `Tokens \| null` | Token counts from opencode's most recent message info — `{input, output}`. `null` on the degraded path. |
| `cost` | `float \| null` | Per-session cost in USD. `0.0` for self-hosted models like local ollama. `null` on the degraded path. |

Empty list (`[]`) is a valid response — user exists but has no live channels (newly seeded user, or post-`/v1/clear`). 404 is reserved for "user_id doesn't exist," so callers can distinguish "valid user, no sessions" from "wrong user."

### Side effects

- None on the database. The endpoint is read-only.
- One `GET /session` call to opencode per request (or zero, if the user has no channels). Failure here is logged but not propagated.

### Errors

- **404** — `user_id` query parameter points at a `users.id` row that doesn't exist. (The override path skips this check by construction — the override is validated at boot.)
- **422** — `user_id` is not a valid integer.

### Examples

```bash
# Default user.
curl -s http://127.0.0.1:7080/v1/sessions | jq

# Specific user.
curl -s 'http://127.0.0.1:7080/v1/sessions?user_id=2' | jq

# Degraded — opencode is down. Same endpoint, partial data.
curl -s http://127.0.0.1:7080/v1/sessions | jq '.[0]'
# {
#   "channel_id": 12,
#   "channel": {...},
#   "oc_session_id": "ses_...",
#   "last_used_at": 1781609548,
#   "title": null,
#   "tokens": null,
#   "cost": null
# }
```

## `POST /v1/sessions/{channel_id}/clear`

Source: `routes/sessions.py:295`.

Drop the `session_links` row for one channel. The `channels` row
itself stays — only the *pointer* to opencode's session is removed.
The opencode session is **never** destroyed by this endpoint
(ADR-13). The next `/v1/chat` to the same channel allocates a fresh
opencode session.

Idempotent on retry: clearing an already-cleared channel returns
`{"cleared": false}` with HTTP 200. The handler queries `channels`
directly (rather than going through `sessions.get_channel`, which
INNER-JOINs `session_links`) so the post-clear state is still
visible to the ownership check.

### Request

Path parameter:

| Param | Type | Meaning |
|-------|------|---------|
| `channel_id` | `int` | The `channels.id` to clear. Source it from `GET /v1/sessions`. |

No body. No query parameters — the principal is implicit:
`SCUFRIS_USER_ID` override if set, otherwise the seeded default
user (`DEFAULT_USER_ID = 1`). Ownership is verified against the
channel row's `user_id`; a channel that exists but belongs to a
different principal returns 404 (D2 — we don't leak channel
existence to non-owners).

### Response

```json
{"cleared": true}
```

| Field | Type | Meaning |
|-------|------|---------|
| `cleared` | `bool` | `true` when a `session_links` row was deleted. `false` on retry — channel exists, belongs to the resolved principal, but had no live link. |

### Side effects

- Deletes one `session_links` row (when `cleared=true`).
- Leaves `channels` and the upstream opencode session intact.

### Errors

- **404** — channel doesn't exist *or* belongs to a different principal. The body is the same in both cases (`{"detail": "channel {N} not found"}`); the WARNING log line on the server distinguishes them for operators.

### Examples

```bash
# Clear channel 12 for the default user.
curl -s -X POST http://127.0.0.1:7080/v1/sessions/12/clear | jq
# {"cleared": true}

# Idempotent retry — same channel, no link left.
curl -s -X POST http://127.0.0.1:7080/v1/sessions/12/clear | jq
# {"cleared": false}

# Channel doesn't exist, or belongs to another user — same 404 either way.
curl -s -X POST http://127.0.0.1:7080/v1/sessions/9999/clear -i | head -1
# HTTP/1.1 404 Not Found
```

## `POST /v1/clear`

Source: `routes/sessions.py:367`.

Drop every `session_links` row for one user, in a single
transaction. The user's `channels` rows survive — only the pointers
go. As with the per-channel endpoint, opencode-side sessions are
**never** destroyed (ADR-13).

The bulk variant of `/v1/sessions/{channel_id}/clear`. Idempotent —
clearing a user with no live links returns `{"count": 0}` with HTTP
200, not a 404.

### Request

```json
{"user_id": 1}
```

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `user_id` | `int` (≥ 1) | yes | The user whose `session_links` to drop. If `SCUFRIS_USER_ID` is set, this body field is silently ignored — the override always wins (D2). Operators running pinned can't accidentally clear another user from a stray curl. |

No query parameters.

### Response

```json
{"count": 1}
```

| Field | Type | Meaning |
|-------|------|---------|
| `count` | `int` | Number of `session_links` rows deleted. `0` on a no-op clear (user exists, no live links — this is success, not 404). |

### Side effects

- Deletes zero or more `session_links` rows in one transaction.
- Leaves `channels` and upstream opencode sessions intact.

### Errors

- **404** — `user_id` (after override resolution) points at a `users.id` row that doesn't exist.
- **422** — body missing `user_id`, or `user_id < 1` (validated by `Field(..., ge=1)` on `BulkClearRequest`).

### Examples

```bash
# Clear all live channels for user 1.
curl -s -X POST http://127.0.0.1:7080/v1/clear \
  -H 'content-type: application/json' \
  -d '{"user_id": 1}' | jq
# {"count": 3}

# Idempotent retry.
curl -s -X POST http://127.0.0.1:7080/v1/clear \
  -H 'content-type: application/json' \
  -d '{"user_id": 1}' | jq
# {"count": 0}

# Unknown user.
curl -s -X POST http://127.0.0.1:7080/v1/clear \
  -H 'content-type: application/json' \
  -d '{"user_id": 9999}' -i | head -1
# HTTP/1.1 404 Not Found
```

## Placeholder routers

The following routers exist as empty `APIRouter` instances. They are
imported by `routes/__init__.py` (so any import-time error shows up
at boot) but they are *not* mounted in the `ROUTERS` list, so they
contribute zero paths to the live app.

When the owning task lands, two things have to happen:

1. Add concrete routes to the `router` defined in the placeholder file.
2. Append `router` to `ROUTERS` in `routes/__init__.py`.

| Module | Owning task | Planned endpoints |
|--------|-------------|-------------------|
| `routes/stats.py`       | `#13` (`tasks/20260613-091047`) | `GET /v1/stats` |
| `routes/permissions.py` | `#30` (`tasks/20260613-093108`) | `POST /v1/permissions/:perm_id/reply` |

The shapes are documented in `tasks/20260613-091036/TASK.md` §8 (the
public-API table). Today, requests under those prefixes return 404.

`routes/sessions.py` was a placeholder until `#10` shipped (now lives
above as `GET /v1/sessions`, `POST /v1/sessions/{channel_id}/clear`,
and `POST /v1/clear`). `#13`'s planned `/v1/clear` was promoted out
of the stats router and lives on the dedicated `clear_router` in
`scufris_server/routes/sessions.py`.

## Future endpoints (not yet wired)

- `POST /v1/chat/stream` — SSE variant of `/v1/chat`. Same request
  shape, response is a stream of `ThinkingEvent`-shaped chunks
  terminating in a `done` event. Owned by `#11`. Will live in
  `routes/chat.py` alongside the synchronous handler.

- `POST /v1/sessions/{channel_id}/fork` — additive, git-branch-style
  fork of an existing channel onto a new opencode session. Optional
  `messageID` body field cuts at a specific message; otherwise forks
  from the latest. Server-mints the fork's `surface_id` as
  `parent.surface_id + "#" + new_oc_id[:8]` so the resulting child
  channel has a stable, recognisable identity. Owned by `#33`
  (`tasks/20260616-111428`). Will land on `sessions_router`.

- `POST /v1/sessions/{channel_id}/expire` (or a parameterised
  variant of `/clear`) — clear-with-server-side-expire that
  additionally calls `DELETE /session/{id}` on opencode. Distinct
  from today's `/clear` precisely because today's `/clear` upholds
  ADR-13 and *never* destroys upstream sessions. Owned by `#34`
  (`tasks/20260616-111430`). Shape and exact spelling still TBD.

- Bearer-token auth gate — `#14` adds `SCUFRIS_TOKEN` checking on
  `/v1/*`. Will probably land as middleware rather than per-route
  dependency.

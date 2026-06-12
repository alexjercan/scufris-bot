# Replace agent builder with OpenCode-native runtime

- STATUS: CLOSED
- PRIORITY: 100
- TAGS: architecture, refactor, opencode

## Outcome (2026-06-10)

Runtime swap landed. `utils/agent_builder.py`, `utils/tools/opencode_tool.py`
and `utils/tools/memory_tools.py` are deleted; `utils/agent.py` now drives
turns through `utils/opencode_client.py` (raw `httpx.AsyncClient`, bypassing
the stale `opencode_ai` SDK) and `utils/opencode_events.py` (event mapper).

`scufris_server/bootstrap.py` builds an `OpenCodeClient` from
`OPENCODE_BASE_URL` / `OPENCODE_PROVIDER_ID` / `OPENCODE_MODEL_ID` (defaults
`http://127.0.0.1:4096`, `github-copilot`, `claude-sonnet-4`); per-call tools
disable `task`/`todoread`/`todowrite` so the assistant doesn't spawn its
own sub-agents. The compactor is wired to `NoopCompactor` until
`tasks/20260610-105002` replaces `LLMCompactor`.

`POST /v1/clear` now also forgets the user's OpenCode session
(`AgentManager.delete_session`, best-effort). New auth-gated proxy routes:
`/v1/opencode/{sessions,sessions/{id},models,providers}` plus
`/v1/readyz` pings both Ollama and OpenCode, and `/v1/version` reports
`opencode_base_url`/`provider`/`model`.

Verified end-to-end against a live `opencode serve` on `:4096`:

- `pytest -q`: 331 passed.
- `ruff check` clean on touched files.
- Direct `AgentManager.process_message` round-trip returned the expected
  text and produced a `kind="text"` thinking event; session created and
  deleted cleanly.
- In-process ASGI smoke: `/v1/healthz`, `/v1/version`, `/v1/readyz`
  (status=ready, ollama=200, opencode=200), `/v1/opencode/sessions`
  (count=25), `/v1/opencode/models`, `/v1/chat` (`{response: 'pong'}`),
  `/v1/clear` (cleared=2, OpenCode session forgotten).
- `/v1/chat/stream` smoke: incremental `thinking` deltas (`"stre"`,
  `"amed"`) followed by a terminal `done` frame with the assembled text.

### Acceptance criteria

- [x] `utils/agent_builder.py` deleted; no module imports it.
- [ ] `langchain*` packages removed from `pyproject.toml` — **deferred**:
      `LLMCompactor` and `ChatHistoryManager`'s window still depend on
      LangChain message classes; concrete blocker list filed as
      `tasks/20260610-105002`.
- [x] `POST /v1/chat` and `POST /v1/chat/stream` produce correct responses
      driven by OpenCode (smoke proven).
- [x] `POST /v1/clear` deletes the OpenCode session for that user.
- [x] Session reuse works (the agent caches `_sessions[user_id]` and reuses
      it across turns; verified via `/v1/opencode/sessions` after the chat
      smoke).
- [x] 404-on-stale-session handled transparently — `OpenCodeClient` raises
      `OpenCodeStaleSessionError`; `AgentManager.process_message` evicts
      the cached id and retries exactly once.
- [x] `/v1/opencode/sessions` and `/v1/opencode/models` proxy correctly,
      auth-gated.
- [x] `/v1/readyz` pings OpenCode (`GET /session`, 2s timeout, 5s cache);
      Ollama ping kept until the compactor migrates.
- [x] `ThinkingEvent` wire format unchanged — `chat.py` still serialises
      the same fields; the new mapper produces `kind="text"` for deltas
      and `kind="tool_call"`/`tool_result` for tool-part transitions.
- [x] Facts and summary still prepended at session start: the agent rebuilds
      the system prompt every turn from
      `DEFAULT_SYSTEM_PROMPT_BASE + facts + summary`.
- [ ] `nix flake check` — **not run from this session** (no NixOS host
      available); test suite + ruff are green and the runtime ran against
      the live daemon. Re-verify under `tasks/20260610-104956` once the
      VM test is wired.
- [x] `tests/test_server.py` updated (added `_StubOpenCodeClient`,
      `_StubAgentManager.delete_session`); deleted `agent_builder` tests
      (`tests/test_sub_agent_memory.py`, `tests/test_memory_tools.py`);
      stripped opencode_tool tests from `tests/test_http_tools.py` (5
      cases) since the helper module went away.

### Deviations from the original plan

- The `opencode_ai` SDK is bypassed entirely (its pydantic event union
  is incomplete); raw dicts on `httpx.AsyncClient` throughout.
- Per-request `GET /event` connection (one per chat turn, gated by an
  `asyncio.Event` so subscribers are open before the POST fires). The
  global fan-out variant is filed as `tasks/20260610-105013`.
- `POST /session/{id}/message` is non-streaming on the wire — the agent
  reconstructs the final text from `message.part.delta.delta`
  concatenation rather than from the POST response body.
- Default port changed from the originally drafted `8080` to `4096`
  (matches the user-running daemon and the SDK fallback we left intact).
- Three docstring-only references to `agent_builder` survive in
  `utils/history.py`, `utils/telemetry.py`, and
  `tests/test_memory_compactor_phase2.py` — they explain still-live
  contracts (sub-agent context injection, `is_refusal` shape) whose
  cleanup belongs to `tasks/20260610-105002`.

### Follow-ups

- `tasks/20260610-104956` (P90) — Nix module + systemd unit for
  `opencode serve`. Hard prereq for deploy.
- `tasks/20260610-105002` (P50) — `LLMCompactor` rewrite; unblocks
  the LangChain removal acceptance criterion above.
- `tasks/20260610-105007` (P40) — persist `user_id → session_id` across
  restarts (today the map is in-memory only).
- `tasks/20260610-105013` (P30) — single global `/event` listener with
  fan-out (replaces per-request connection).
- `tasks/20260610-105018` (P30) — swap `/v1/stats` to OpenCode-reported
  cost/tokens.

## Original task description (preserved)

### Motivation

The current `utils/agent_builder.py` is the heaviest single piece of the
codebase: it builds LangChain agents, wires sub-agent tools, manages
callback chains, implements per-agent history injection, and composes
context strings. That's a lot of infrastructure we built from scratch
for things OpenCode already handles — tool dispatch, agent loops,
conversation context, skill loading.

The goal of this task is to **replace the LangChain-based agent stack
with OpenCode as the runtime**, keeping `scufris-server`'s HTTP API as
the stable client contract. From the outside (CLI, Telegram bot) nothing
changes. Internally, a request that previously drove a LangChain
`create_agent` loop now drives an OpenCode session.

The sub-agent hierarchy (coding, knowledge, journal, utilities) is
**not** being ported. Those responsibilities move to OpenCode skills —
filed as separate tasks. This task only covers the runtime swap itself.

## Architecture after this task

```
scufris-server (FastAPI, unchanged)
│
├── /v1/chat, /v1/chat/stream   ← same endpoints
├── /v1/stats, /v1/clear        ← same
└── /v1/opencode/*              ← new thin proxy (see below)
│
▼
OpenCode daemon  (opencode serve, local HTTP)
│
└── skills/  (AGENTS.md + per-skill .md files — separate tasks)
```

`AgentManager.process_message` no longer calls
`self.agent.invoke(...)`. Instead it makes a (non-streaming) chat
POST to OpenCode and, in parallel, consumes OpenCode's
*server-global* SSE event bus (`GET /event`), filtering events for
the in-flight session and mapping them into `ThinkingEvent`s for the
existing SSE renderer. See `SCHEMA.md` (sibling file) for the full
event taxonomy, real wire samples, and the mapping table.

`utils/agent_builder.py` is **deleted**. The LangChain dep can be
removed from `pyproject.toml` once no other module imports it (verify
first — `ToolCallbackHandler` and `ThinkingEvent` may need to survive
in a trimmed form).

## Scope

### In

**1. OpenCode client wrapper (`utils/opencode_client.py`)**

A thin async HTTP client around `opencode serve`'s REST API. Reads the
base URL from `OPENCODE_BASE_URL` (no port baked into Python — the
daemon supervision task `tasks/20260610-104956` pins it). Covers at
minimum:

- `create_session()` → session dict (system prompt is per-message, not
  per-session — see Section 2)
- `chat_stream(session_id, message, *, system, provider_id, model_id, tools?)`
  → async iterator of raw event dicts. Opens `GET /event` in parallel
  with `POST /session/{id}/message`; terminates on `session.idle` /
  `session.error` for the in-flight session.
- `delete_session(session_id)`
- `list_sessions()` (for stats / proxy)

Use `httpx.AsyncClient` (already a dep via `scufris-server`). **Do
not use the `opencode_ai` SDK** for parsing events — its pydantic
types omit several event types (`message.part.delta`, `session.created`,
`session.diff`, `session.status`, `session.next.*`) and several
`Session` fields the live server emits. Raw `httpx` + `dict` access
throughout. The existing sync `utils/tools/opencode_tool.py` is *not*
a useful pattern — it's blocking, uses the SDK, and points at the
wrong port (`4096`).

**2. Rewrite `AgentManager` (`utils/agent.py`)**

- Drop the LangChain `Runnable` field.
- Replace `process_message(messages, user_id)` internals: open an
  OpenCode session per `(user_id)` (or reuse a cached one — see
  session lifecycle below), send the message, stream events.
- Map OpenCode's event stream to `ThinkingEvent`s. Full table in
  `SCHEMA.md`; summary of the mappings the mapper must implement:
  - `message.part.delta` with `field == "text"` → `kind="text"`,
    `source="scufris"`, `text=delta`. Append-style, no per-part
    bookkeeping needed.
  - `message.part.updated` with `part.type == "tool"` and
    `state.status == "running"` (first occurrence per `part.id`) →
    `kind="tool_call"`, `text=part.tool`, `arg=state.title`. Dedup by
    `part.id`.
  - `message.part.updated` with `part.type == "tool"` and
    `state.status == "completed"` → `kind="tool_result"` (suppressed
    by default in the renderer; keep that policy).
  - `session.idle` (matching our session) → terminator. The final
    reply text is the concatenation of every `message.part.delta.delta`
    seen for the in-flight assistant `messageID`.
  - `session.error` (matching our session) → terminator + raise; map
    `error.name` to a user-facing string.
  - All other event types: ignore (see SCHEMA.md for the full list).
- History is now **OpenCode's responsibility** within a session. The
  `ChatHistoryManager` window/summary/facts layers are preserved for
  `user_id`-keyed facts and stats — but the raw message window is no
  longer fed into every invocation. Decide: either (a) send recent
  window as context on session create, or (b) trust OpenCode's session
  to hold it. Recommendation: **(b)** — that's the point of the swap.
  Facts and summary still get prepended as a system message at session
  start.

**3. Session lifecycle**

OpenCode sessions are stateful. Define the mapping:

- One persistent session per `user_id` (created on first message,
  reused until explicitly cleared). Session ID stored in a small
  `_sessions: Dict[int, str]` on `AgentManager`.
- `POST /v1/clear` deletes the OpenCode session (in addition to wiping
  `ChatHistoryManager`) and removes it from the map so the next message
  starts fresh.
- Server restart loses the in-memory map → next message creates a new
  session. Acceptable for v1; document.
- When OpenCode returns a 404 on a cached session (was garbage-collected
  on its side), catch and recreate transparently.

**4. New `/v1/opencode/*` proxy endpoints**

Expose a thin pass-through so the CLI and future clients can access
OpenCode features directly without knowing the internal port:

| Method | Path | Proxies to |
|--------|------|-----------|
| GET | `/v1/opencode/sessions` | `GET /session` |
| GET | `/v1/opencode/sessions/{id}` | `GET /session/{id}` |
| DELETE | `/v1/opencode/sessions/{id}` | `DELETE /session/{id}` |
| GET | `/v1/opencode/models` | `GET /models` |
| GET | `/v1/opencode/providers` | `GET /providers` |

Auth-gated by the same `SCUFRIS_TOKEN` bearer check as all other
endpoints. These are read/inspect endpoints only — chat still goes
through `/v1/chat`.

**5. `ToolCallbackHandler` and `ThinkingEvent` — trimmed, not deleted**

`ThinkingEvent` is the SSE wire format consumed by `scufris-cli` and
`bot.py`. Keep it. `ToolCallbackHandler` as a LangChain callback class
is deleted; replace with a plain function `map_opencode_event(raw_event)
→ ThinkingEvent | None` in `utils/callbacks.py` (or a new
`utils/opencode_events.py`).

**6. Remove or thin out LangChain**

After `agent_builder.py` is gone, audit imports. Goal: remove
`langchain`, `langchain-core`, `langchain-community`, `langchain-ollama`
from `pyproject.toml` if nothing else pulls them in. If something still
needs them (unlikely), file a follow-up to remove that dependency too.

**7. Update bootstrap**

`scufris_server/bootstrap.py` and `cli.py` build the `Runtime` today by
calling `setup_scufris(config, history_manager)`. After this task,
`Runtime` holds an `AgentManager` backed by the OpenCode client and a
`ChatHistoryManager` for facts/stats only. Update `build_runtime`
accordingly.

**8. Update `/v1/readyz`**

Currently pings Ollama. Add a second check: ping `opencode serve`'s
health endpoint. Both must pass for readiness. If OpenCode has no health
endpoint, a `GET /models` with a short timeout is sufficient.

### Out

- Skills implementation (knowledge, journal, etc.) — separate tasks.
- Migrating `ChatHistoryManager`'s compactor — filed as
  `tasks/20260610-105002`. Until that lands, swap in `NoopCompactor`.
- Persistent session storage across restarts — filed as
  `tasks/20260610-105007`.
- Daemon supervision (Nix module + systemd unit for `opencode serve`)
  — filed as `tasks/20260610-104956`. **Hard prereq for deployment**
  but not for landing the code change.
- Changes to `scufris-cli` or `bot.py` (they talk HTTP; the swap is
  transparent to them).

## Acceptance criteria

- [ ] `utils/agent_builder.py` is deleted. No file in the repo imports
      it.
- [ ] `langchain*` packages are removed from `pyproject.toml` (or a
      follow-up task is filed with a concrete list of remaining
      blockers).
- [ ] `POST /v1/chat` and `POST /v1/chat/stream` produce correct
      responses driven by OpenCode.
- [ ] `POST /v1/clear` deletes the OpenCode session for that user.
- [ ] Session reuse works: two successive `/v1/chat` calls for the same
      `user_id` land in the same OpenCode session (verify via
      `GET /v1/opencode/sessions`).
- [ ] 404-on-stale-session is handled transparently (recreate and
      retry).
- [ ] `/v1/opencode/sessions` and `/v1/opencode/models` proxy correctly
      and are auth-gated.
- [ ] `/v1/readyz` pings OpenCode (a fast `GET /session` works as a
      health check). Keep the Ollama ping as long as the compactor
      still uses it; remove together with `tasks/20260610-105002`.
- [ ] `ThinkingEvent` wire format is unchanged — `scufris-cli` renders
      the same thinking trace it did before.
- [ ] Facts and summary from `ChatHistoryManager` are still prepended
      to new sessions (so `remember`/`forget` tools still work).
- [ ] `nix flake check` passes (ruff, mypy, pytest, VM test).
- [ ] Existing tests that stub `AgentManager` are updated; deleted
      `agent_builder` tests are removed.

## Open questions (decide before or during implementation)

- **System prompt delivery**: OpenCode sessions take a system prompt at
  create time. The current `MAIN_AGENT_PROMPT` (in `agent_builder.py`)
  is long and references the sub-agent delegation contract — most of
  that is obsolete once skills replace sub-agents. For this task: pass
  a minimal system prompt ("You are Scufris, a personal assistant.
  Known facts: {facts}. Summary: {summary}.") and leave full prompt
  design to the skills tasks.
- **OpenCode event schema**: SPIKED — see `SCHEMA.md` (sibling file).
  Highlights for anyone reviewing this task: chat is non-streaming
  (`POST /session/{id}/message` blocks until the model finishes);
  events arrive on a server-global `GET /event` SSE bus and must be
  filtered by `properties.sessionID`; the per-token text event is
  `message.part.delta` (NOT `message.part.updated`); `session.idle`
  is the turn terminator; the SDK's typed event union is incomplete,
  so the implementer must bypass it for the event stream.
- **One session per user vs. one per (user, surface)**: if Alex uses
  CLI and Telegram simultaneously, both surfaces share one session.
  That means interleaved messages from two surfaces appear in one
  conversation. Probably fine and actually desirable (matches the
  identity-unification goal), but call it out explicitly.

## References

- `SCHEMA.md` (sibling file) — wire-format spike output; authoritative
  for event shapes and the mapper design.
- `utils/agent_builder.py` — deleted by this task.
- `utils/agent.py` — `AgentManager`, rewritten.
- `utils/callbacks.py` — `ThinkingEvent`, trimmed.
- `utils/tools/opencode_tool.py` — existing sync SDK usage; reference
  only — its port (`4096`) and event-handling assumptions are stale.
- `scufris_server/bootstrap.py` — `build_runtime`, updated.
- `scufris_server/routes/chat.py` — SSE bridge, updated to use new
  event mapper.
- `tasks/20260513-121625/TASK.md` — skills spike (the follow-on work
  this task unblocks).
- `tasks/20260610-104956` — OpenCode daemon supervision (deploy
  prereq).
- `tasks/20260610-105002` — LLMCompactor rewrite (LangChain removal
  prereq).
- `tasks/20260610-105007` — persistent session map (v2).
- `tasks/20260610-105013` — event listener fan-out (v2 perf).
- `tasks/20260610-105018` — stats source swap (v2).

# Spike: opencode as agent runtime — sessions, tools, plugin API, config model

- STATUS: CLOSED
- PRIORITY: 95
- TAGS: spike,design,opencode

Survey what opencode actually exposes (session create/list, tool
registration, streaming events, multi-agent/model config) before
committing to an architecture. Output is a short capabilities note +
recommendation that feeds the design doc (next task). Blocks almost
everything else in the v2 backlog.

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).

---

## Findings

### What was probed

- Live `opencode serve` v1.15.13 on `127.0.0.1:4096`, project
  `/home/alex/personal/scufris-bot` (id
  `4a3330808d648329fb786342d40c51340019e692`).
- Full OpenAPI spec dumped from `GET /doc` (283 KB JSON, OpenAPI 3.1.0,
  ~140 paths, ~600 schemas).
- End-to-end probe: created a session, sent a trivial no-tools message
  via `ollama/qwen3:latest`, captured 115 SSE events on `/event` while
  the message ran, then ran a structured-output probe with
  `format: { type: "json_schema", schema: ... }`.
- Cross-checked against `opencode.ai/docs` for `server`, `sdk`,
  `agents`, `custom-tools`, `plugins`, `config`, `mcp-servers`,
  `permissions`.

### HTTP surface (legacy + v2)

opencode exposes two parallel session APIs. The v2 path (`/api/session/*`)
is newer and looks like the recommended path going forward; the legacy
path (`/session/*`) still works and is what the OpenAPI examples
mostly use today.

| Path | Verbs | Notes |
|------|-------|-------|
| `/session` | GET, POST | List/create. POST body can be empty; returns `Session` with `id`, `slug`, `projectID`, `directory`, `tokens`, `time`. |
| `/session/{id}` | GET, PATCH, DELETE | Read/update/delete. PATCH for title etc. |
| `/session/{id}/message` | GET, POST | **Synchronous** send: returns the completed assistant `{info, parts}` after the agent loop finishes. |
| `/session/{id}/prompt_async` | POST | Async send: enqueues, returns immediately. |
| `/session/{id}/abort` | POST | Cancel the current loop. |
| `/session/{id}/summarize` | POST | Run the built-in `summary` agent. |
| `/session/{id}/init` | POST | Analyze repo and write `AGENTS.md`. |
| `/session/{id}/fork` | POST | Branch a session at a message point — new session inherits prefix. |
| `/session/{id}/revert`, `/unrevert` | POST | Undo/redo to a message (uses git snapshot hash on each step). |
| `/session/{id}/diff` | GET | File diff resulting from a specific user message. |
| `/session/{id}/share` | POST, DELETE | Publish a read-only share link. |
| `/session/{id}/children` | GET | Subtask sessions spawned via the `task` tool. |
| `/session/{id}/command` | POST | Run a slash command. |
| `/session/{id}/shell` | POST | Run a shell command in session context. |
| `/session/{id}/todo` | GET | Read the session's todo list (the `todowrite` tool's state). |
| `/session/{id}/permissions/{permID}` | POST | Reply to a permission prompt (allow/once/deny). |
| `/session/{id}/message/{msgID}/part/{prtID}` | PATCH, DELETE | Granular part edit/remove. |
| `/api/session` | GET | v2 list with cursor pagination. |
| `/api/session/{id}/prompt` | POST | v2 queued send (returns immediately). |
| `/api/session/{id}/wait` | POST | Block until agent loop is idle. |
| `/api/session/{id}/context` | GET | Active context window (messages since last compaction) — exactly the "what is the LLM seeing right now" view. |
| `/api/session/{id}/compact` | POST | Trigger compaction explicitly. |
| `/event`, `/global/event` | GET (SSE) | Event stream, scoped per-project vs. global. |
| `/agent` | GET | List configured agents (built-in + user). |
| `/provider` | GET | Providers + models + default-model recommendations + `connected[]`. |
| `/experimental/tool` | GET | Per-(provider, model) tool list with full JSON Schema parameters. |
| `/experimental/tool/ids` | GET | Just the tool IDs. |
| `/mcp` | GET, POST, DELETE | List / register-at-runtime / remove MCP servers. |
| `/config` | GET, PATCH | Active config; PATCH is partial. **Not hot-reloaded for `opencode.json` changes — restart required.** |
| `/project`, `/project/current` | GET | Multi-project registry; project = cwd at server start. |
| `/log` | POST | Forward client logs into opencode's logger. |
| `/auth/{providerID}` | PUT, DELETE | Set/clear provider credentials. |

### `POST /session/:id/message` request shape (the hot path)

```jsonc
{
  "messageID": "msg_...",            // optional, server generates if omitted
  "model": { "providerID": "ollama", "modelID": "qwen3:latest" },
  "agent": "build",                  // any agent id from /agent
  "system": "string",                // PER-MESSAGE system prompt override
  "tools": { "bash": false, "read": true, ... },  // PER-MESSAGE allowlist override
  "format": {                        // optional; structured output
    "type": "json_schema",
    "name": "person",
    "schema": { ... },
    "strict": true
  },
  "noReply": false,                  // suppress assistant reply
  "variant": "string",               // model variant override
  "parts": [
    { "type": "text", "text": "..." },
    { "type": "file", "mime": "image/png", "url": "...", "filename": "..." },
    { "type": "agent", "name": "research" },              // mention an agent in-line
    { "type": "subtask", "agent": "research", "prompt": "...", "description": "..." }
  ]
}
```

Response: `{ info: AssistantMessage, parts: Part[] }` — ready to render.

### Streaming / event vocabulary

`GET /event` is a long-lived SSE stream. Each event is one JSON object
with `id`, `type`, `properties`. ~80 distinct types, all enumerated in
the OpenAPI as a discriminated union. For our trivial probe (single
text reply, no tools) the order was:

```
server.connected               (on connect)
session.created                (one-shot)
session.updated                (mutation snapshots; ~6x)
message.updated  (user msg)
message.part.updated  (user text part)
session.next.agent.switched    {agent: "build"}
session.next.model.switched    {model: {id, providerID, variant}}
session.status                 {status: {type: "busy"}}
message.updated  (assistant skeleton)
message.part.updated           (step-start part)
message.part.updated           (reasoning part)
message.part.delta x84         {field: "text", delta: "Okay"} for token-by-token text
message.part.updated           (text part finalized)
message.part.updated           (step-finish part)
session.diff x2                {diff: SnapshotFileDiff[]}
session.idle
session.status                 {status: {type: "idle"}}
server.heartbeat               (periodic)
```

Other event families documented (didn't fire for trivial case):

- **Streaming detail** (`session.next.*`): `text.{started,delta,ended}`,
  `reasoning.{started,delta,ended}`,
  `tool.{input.started,input.delta,input.ended,called,success,failed,progress}`,
  `step.{started,ended,failed}`,
  `compaction.{started,delta,ended}`, `shell.{started,ended}`,
  `synthetic`, `prompted`, `retried`. These appear to be the
  finer-grained "what the loop is doing right now" events. For UI
  rendering, `message.part.delta`/`message.part.updated` are sufficient
  — the `session.next.*` set looks aimed at instrumentation/sync.
- **Permission flow**: `permission.asked`, `permission.replied`,
  `question.{asked,replied,rejected}`.
- **External signals**: `file.edited`, `file.watcher.updated`,
  `vcs.branch.updated`, `lsp.{updated,client.diagnostics}`,
  `mcp.tools.changed`, `command.executed`, `pty.{created,updated,exited,deleted}`,
  `worktree.{ready,failed}`, `workspace.{ready,failed,status}`,
  `project.updated`, `installation.{updated,update-available}`,
  `account.{added,removed,switched}`, `catalog.model.updated`,
  `models-dev.refreshed`, `plugin.added`.
- **Sync** (`SyncEvent*` schemas, `type: "sync"`): replicated session
  events for multi-instance state sync. Wraps a payload identical to
  the `Event*` variant.
- **TUI** (`tui.*`): control events targeted at the TUI client
  (`prompt.append`, `command.execute`, `session.select`, `toast.show`).

### Structured output

`format: { type: "json_schema", schema, strict }` is implemented as a
synthetic tool the runtime injects, named `StructuredOutput`. The model
calls it with the structured payload as the tool input. Result:

- `info.structured` holds the parsed-and-validated object directly.
- The corresponding part is `{type: "tool", tool: "StructuredOutput",
  state: {status: "completed", input: <obj>, metadata: {valid: true}}}`.
- `info.finish: "tool-calls"` (not `"stop"`).

Validation is server-side. v1's hand-rolled `parser`/regex extraction
is fully obsoleted by this.

### Agents

- `GET /agent` returns built-ins + user. Locally we have `build`
  (primary, native) plus the hidden built-ins `compaction`, `summary`,
  `title`. Agents are configured in `opencode.json`, `.opencode/agent/*.md`,
  or `AGENTS.md`.
- Agent fields: `mode` (`primary | subagent | all`), `model`, `prompt`,
  `temperature`, `top_p`, `maxSteps`, `permission` (per-tool /
  per-pattern allow/ask/deny), `tools` (allowlist), `color`. Per-agent
  permission overrides global.
- The `task` tool dispatches to subagents — fully replaces v1's
  `create_sub_agent`.
- `agent`-typed message parts and `subtask`-typed parts let a single
  message reference/spawn agents inline.

### Tools

Built-in: `invalid, question, bash, read, glob, grep, edit, write,
task, webfetch, todowrite, websearch, skill, apply_patch`. Each
returned by `/experimental/tool` with `{id, description, parameters: <JSON Schema 2020-12>}`.

Three ways to add tools:

1. **`.opencode/tool/*.ts`** custom tools — TS files with a default
   export of `{ args: ZodSchema, execute: (args) => result }`.
   In-process, hot-loaded on session start.
2. **Plugin tools** — published as a plugin (npm package or local
   `.opencode/plugin/*.ts`) exporting a hook that registers tools.
3. **MCP servers** — declared statically in `opencode.json`
   (`mcp.<name>: {type: "local"|"remote", ...}`) or registered at
   runtime via `POST /mcp`. Tools surface under `mcp__<server>__<tool>`.

### Plugins

Plugins are TS modules with an exported default object exposing hooks.
Hook surface includes:

- `event` (intercept SSE events).
- `tool.execute.before`, `tool.execute.after`.
- `permission.ask`.
- `chat.message`, `chat.params`.
- `auth`.
- `experimental.session.compacting` — runs at compaction, can inject
  context.

Hooks can register custom tools, custom MCP servers, custom commands.

### Config / projects / auth

- Single `opencode.json` (global `~/.config/opencode/`, or per-project
  in repo). Schema covers providers, agents, MCP servers, commands,
  permissions, compaction tuning (`compaction.auto`, `tail_turns`),
  formatters, etc. Not hot-reloaded.
- A project = the directory containing the `.git` root that opencode
  finds at startup. Sessions are project-scoped and persist across
  restarts under the project's id.
- Auth: opencode-side server is gated by HTTP basic via
  `OPENCODE_SERVER_PASSWORD` (single shared secret, no per-user). For
  any multi-tenant use, identity must live in our layer in front of
  opencode.
- No official Python SDK — only `@opencode-ai/sdk` (TS). For a Python
  v2 server we either generate a client from `/doc` or hand-roll
  `httpx` calls.

---

## Capability matrix vs. v1 / v2 backlog

Legend: **own** = opencode owns this; **integrate** = thin glue;
**ours** = scufris-v2 must build this; **gone** = v1 hand-rolled, can
delete.

| Capability                          | opencode | v1 status               | v2 implication                                                  |
|-------------------------------------|----------|-------------------------|-----------------------------------------------------------------|
| Agent loop (LLM ↔ tools)            | own      | hand-rolled             | gone — call `/session/:id/message`                              |
| Tool registry / dispatch            | own      | LangChain `@tool`       | gone — register via custom-tools dir or plugin                  |
| Sub-agent delegation                | own      | `create_sub_agent`      | gone — the `task` tool + `subtask` parts                        |
| Per-agent prompts/models/perms      | own      | partial                 | gone — `agent` config + per-message override                    |
| Structured output                   | own      | regex parser            | gone — `format.json_schema`, see `info.structured`              |
| Streaming responses                 | own      | partial                 | gone — `message.part.delta` over SSE                            |
| Compaction                          | own      | nothing                 | mostly gone — `compaction` agent + `experimental.session.compacting` plugin hook for our facts injection |
| Session persistence                 | own      | per-channel JSON        | gone — opencode persists per project; map (channel→sessionID) is ours |
| Session fork / revert / diff        | own      | none                    | bonus — could expose "branch this convo" UX                     |
| Permissions (per-tool / per-path)   | own      | none                    | gone — last-match-wins allow/ask/deny rules                     |
| Permission UX (ask user)            | own (event) | none                | integrate — relay `permission.asked` → Telegram inline buttons → `POST /session/:id/permissions/:permID` |
| MCP servers                         | own      | none                    | integrate — declare in `opencode.json` or `POST /mcp`           |
| Project scoping                     | own      | none                    | decide — see Decision A below                                   |
| Multi-user / identity               | none     | per-user JSON files     | **ours** — opencode auth is single-secret                       |
| Telegram I/O, channels, rate-limits | none     | own                     | **ours**                                                        |
| Channel ↔ session map               | none     | implicit                | **ours** — durable mapping in scufris DB                        |
| Per-user facts / long-term memory   | none     | scratch                 | **ours** — store in scufris DB; inject via `experimental.session.compacting` plugin and/or per-message `system` override |
| Observability / metrics / cost      | partial  | none                    | partial — opencode emits tokens/cost per message; aggregation/dashboards are ours |
| Audit log                           | none     | none                    | **ours** — capture SSE stream per channel                       |
| Health / readiness                  | partial  | none                    | mostly ours — opencode has `/global/health` only                |

---

## Recommended integration shape for scufris-v2

**Decision A: one opencode process per project, scufris-server fronts
them.** opencode binds project to cwd-at-startup. Run one
`opencode serve` per project we care about, register them in scufris
config. Default deployment: a single process for the bot's working
directory; multi-project comes later.

**Decision B: scufris-server in Python, talks HTTP+SSE to opencode.**
Hand-rolled `httpx.AsyncClient` for now (the OpenAPI spec is small
enough that we can codegen a client later). Long-lived `httpx-sse` /
`aiohttp` consumer per active session for streaming.

**Decision C: scufris owns identity, channels, mapping.** Per-user /
per-channel data lives in a scufris-side SQLite (later Postgres). The
mapping is `(platform, chat_id, user_id) → opencode_session_id`. We
fork or create new sessions on demand.

**Decision D: prefer opencode-native config over scufris-side
config.** Agents, tools, MCP servers, permissions all live in
`opencode.json` / `.opencode/`. We don't reinvent these. scufris config
is limited to: providers/secrets routing, telegram tokens, channel
policy, identity, plugin enablement.

**Decision E: facts and per-user prefs are injected via the
`experimental.session.compacting` plugin hook plus per-message
`system` / `parts` overrides.** This way the per-user context is
visible to the model on every turn without us managing the full
context window.

**Decision F: structured output goes through opencode's
`format.json_schema`.** Delete v1's parser. Validation lives there.

**Decision G: render UI from `message.part.delta` +
`message.part.updated`.** Skip the `session.next.*` family unless we
need fine-grained instrumentation.

---

## Backlog impact (revised priorities)

References point at the planning doc table in
`tasks/20260613-085701/TASK.md`.

| # | Task                                  | New status                                                                                    |
|---|---------------------------------------|------------------------------------------------------------------------------------------------|
| #2 (`...091036`)  | Architecture design doc       | unblocked; this spike is its primary input                                                     |
| #3 (`...091037`)  | Per-(user, agent) sessions    | **shrink** — opencode owns sessions; we own only the (user, channel) → sessionID map          |
| #10 (`...091044`) | Tool registry                 | **delete** — register via `.opencode/tool/*.ts` or plugin                                     |
| #18 (`...091052`) | SQLite session store          | **shrink** — only stores the (channel→session) map + identity, not session state              |
| #19 (`...091053`) | Per-user facts/memory         | **keep** — implement as scufris DB + opencode plugin hook for context injection                |
| #20 (`...091054`) | Compaction strategy           | **delete** — opencode owns compaction; inject via `experimental.session.compacting`            |
| #28 (`...091103`) | Server observability          | **keep** — opencode emits per-message tokens/cost; aggregation, per-user budgets are ours      |
| #29 (`...091104`) | Perf baseline                 | **keep** — measure SSE latency, message round-trip, opencode→ollama p50/p95                    |

New gap surfaced by the spike (worth filing as a follow-up task before
implementation begins):

- **#30 Permissions UX bridge** — relay opencode's `permission.asked`
  events into Telegram inline-keyboard prompts and post user replies
  back to `POST /session/:id/permissions/:permID`. Needed before any
  destructive tool is enabled. Probably belongs near #4 (Telegram
  adapter).

---

## Open questions for the design doc (#2)

1. One opencode process per project, or shared? Default to one for v0,
   but how do we expose more projects later — start opencode children
   from scufris-server, or operate them out-of-band?
2. SSE consumer lifecycle: one persistent consumer per scufris-server
   process, dispatching to per-channel queues? Or one consumer per
   active session?
3. Where does our plugin live — npm-published TS package, or just a
   `.opencode/plugin/scufris.ts` checked into the repo? The latter is
   simpler for v0.
4. Identity/auth: we'll need to gate scufris-server's HTTP API; do we
   reuse Telegram auth tokens or hand-roll JWT?
5. Multi-user history: do we expose per-user fork-from-shared-context
   semantics (using `/session/:id/fork`), or keep one session per
   (user, channel)?

---

## Artifacts produced

- `/tmp/opencode-openapi-raw` — raw OpenAPI 3.1 dump (283 KB).
- `/tmp/oc-probe/message.json` — sample synchronous-message response.
- `/tmp/oc-probe/structured.json` — sample structured-output response.
- `/tmp/oc-probe/events.sse` — SSE capture for a trivial round-trip
  (115 events, 12 distinct types).

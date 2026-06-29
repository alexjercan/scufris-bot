# 000 — Overview

This `docs/` folder is the entry point for understanding scufris-v2.
Read these files in order if you're new to the project.

## What is Scufris?

> Scuffed Jarvis.

Scufris is a personal-assistant daemon. Today's deployment is
single-host, single-user: it runs as a long-lived process on the
user's own machine and exposes a stable HTTP API to user-facing
clients (a CLI, a Telegram bot, eventually a web frontend).

The hard work — running an LLM agent loop, executing tools, managing
permissions, doing context compaction — is delegated to
[opencode](https://opencode.ai). Scufris does not implement any of
that. Scufris's job is to be the *integration layer*: identity,
session ↔ channel mapping, per-user facts, scheduling, audit log,
plus the HTTP/SSE surface that the clients actually talk to.

## Two halves of this repo

```
scufris-bot/
├── scufris/         # v1 — legacy, mostly empty stubs (bot.py, cli.py, server.py)
├── scufris_server/  # v2 — the FastAPI daemon. This is where work happens.
├── tests/
├── tasks/           # tatr task tracker — open + closed work units
└── docs/            # you are here
```

**v1** was a self-contained LangChain-based agent. v1 source files
(`bot.py`, `cli.py`, `server.py`) still exist but are 0 bytes — the
implementation has been deleted as v2 matured. The v1 design docs
under `tasks/20260520-145231/` and `tasks/20260603-132426/` are
preserved for reference (notably the `config.toml` shape, which v2
carries over verbatim).

**v2** lives in `scufris_server/`. It is the FastAPI daemon that
fronts `opencode serve` and exposes `/v1/*` to clients. Every doc in
this folder is about v2 unless explicitly stated.

## Current scope (as of #14 closing)

What's implemented today:

- FastAPI app with structured-JSON logging, request-id middleware,
  SQLite-backed migrations.
- Identity layer (`#12`): TOML-driven user resolution + a
  `SCUFRIS_USER_ID` server-side override.
- Endpoints: `GET /v1/healthz`, `GET /v1/version`,
  `POST /v1/identity/resolve`, `POST /v1/chat/stream`,
  `GET /v1/sessions`,
  `POST /v1/sessions/{channel_id}/clear`, `POST /v1/clear`.
- Async opencode client wrapping a useful subset of opencode's HTTP
  API (health, default-model probe, create_session, send_message,
  long-lived `/event` SSE consumer).
- Synchronous and streaming chat paths sharing one channel ↔ session
  cache in SQLite. The streaming path consumes a per-process
  `EventBus` that reads opencode `/event` once and fans out to
  subscribers (ADR-10).
- v2 CLI client — [DONE] task `#14` (`tasks/20260613-091049`).

What's *not* implemented yet (placeholder modules exist):

- Stats / per-user telemetry — task `#13`
  (`tasks/20260613-091047`).
- Permission UX bridge — task `#30` (`tasks/20260613-093108`).
- Channel fork (`POST /v1/sessions/{id}/fork`) — task `#33`
  (`tasks/20260616-111428`).
- Channel server-side expire — task `#34`
  (`tasks/20260616-111430`).
- The in-tree opencode plugin (`.opencode/plugin/scufris.ts`) — see
  design doc §5.3.

## How to read these docs

| File | When to read it |
|------|----------------|
| [`001_architecture.md`](001_architecture.md)        | First. The 3-process topology, request flow, lifespan startup. |
| [`002_api_reference.md`](002_api_reference.md)      | When you want to call the server. Every endpoint, every field. |
| [`003_identity_and_users.md`](003_identity_and_users.md) | When you need to understand "who is this request from?" — surfaces, resolver, the four steps. |
| [`004_configuration.md`](004_configuration.md)      | When you want to deploy / override defaults. Env vars, `config.toml`, paths. |
| [`005_data_model.md`](005_data_model.md)            | When you're about to write a query or a migration. SQLite schema. |
| [`006_development.md`](006_development.md)          | When you start contributing. Devshell, tests, ruff/mypy, conventions. |

If you're spelunking a bug in a specific path, jump straight to the
endpoint in `002_api_reference.md` and follow the cross-references
back into the codebase.

## Other places to look

- [`README.md`](../README.md) — user-facing run instructions, smoke
  test, how to run integration tests.
- `tasks/20260613-091036/TASK.md` — the master design doc for v2.
  Long, but it's the single source of architectural truth. The numbered
  sections in this doc set crib heavily from §4–14 there.
- `tasks/<id>/TASK.md` (any non-CLOSED task) — the unit of work each
  upcoming feature is tracked under. Run `tatr list` to see the
  current backlog.

## Conventions used in these docs

- Code references use `path/to/file.py:line_number`. Click in your
  editor to navigate.
- `→` means "leads to" / "produces". `vs.` means "as opposed to".
- "v1" and "v2" are the two generations; we're firmly in v2.
- "Surface" is the user-facing channel (cli, telegram, web).
  "surface_id" is the per-surface identifier (a terminal username,
  a Telegram chat_id, a web tab id).
- Endpoints and pydantic models are bolded the first time they're
  introduced and lowercased thereafter.

## Caveats

These docs reflect the codebase **as of 2026-06-20** (after `#10`,
`#11`, and `#12` closed — sessions, SSE streaming, and identity all
landed). They will drift as `#13` / `#14` / `#30` / `#33` / `#34`
land new endpoints and rewrite the lifespan. When in doubt, the
source is authoritative — every claim here should be cross-
referenceable to a file in `scufris_server/`.

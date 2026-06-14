# Scufris

[![CI](https://github.com/alexjercan/scufris-bot/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/alexjercan/scufris-bot/actions/workflows/ci.yml)

Scuffed Jarvis.

## Running scufris-server

`scufris-server` is the v2 FastAPI daemon that wraps `opencode serve`
and exposes a stable HTTP API to user-facing clients (CLI, Telegram).
The repo also still contains the v1 standalone bot — see the
`scufris/` package for that. v2 lives in `scufris_server/`.

### Prerequisites

- Python 3.13 (project pins `>=3.13`); `uv` for dependency management.
- `opencode` 1.15.13 or newer, configured with at least one connected
  provider that has a default model. Local development assumes
  `ollama` on `http://localhost:11434/v1` with `qwen3:latest` pulled.
  See the [opencode docs](https://opencode.ai/docs/) for installing
  and configuring providers.

### Starting the stack

In two terminals (or with your process supervisor of choice):

```bash
# Terminal A — opencode HTTP API on 127.0.0.1:4096 by default.
opencode serve

# Terminal B — scufris-server on 127.0.0.1:7080 by default.
uv run python -m scufris_server
```

`scufris-server` probes `opencode` at startup. The boot log emits a
single `INFO opencode reachable: version=X.Y.Z` line on success and
caches the default model (e.g. `ollama/qwen3:latest`). If `opencode`
is unreachable the server still starts but `/v1/chat` will 503 with
`error_type=DefaultModelMissing`.

### Configuration

All settings have sensible defaults. Override via env vars:

| Variable                    | Default                  | Notes                                                 |
| --------------------------- | ------------------------ | ----------------------------------------------------- |
| `SCUFRIS_BIND`              | `127.0.0.1`              | Interface to listen on.                               |
| `SCUFRIS_PORT`              | `7080`                   | TCP port.                                             |
| `SCUFRIS_STATE_DIR`         | `$XDG_STATE_HOME/scufris`| SQLite DB lives here as `scufris.sqlite`.             |
| `OPENCODE_URL`              | `http://127.0.0.1:4096`  | Where `opencode serve` is reachable.                  |
| `OPENCODE_SERVER_PASSWORD`  | _(unset)_                | Sent as Bearer token if `opencode` requires one.      |

### Smoke test

```bash
# Liveness + opencode reachability.
curl -s http://127.0.0.1:7080/v1/healthz | jq
# {
#   "ok": true,
#   "opencode": { "healthy": true, "version": "1.15.13" }
# }

# Server + opencode versions.
curl -s http://127.0.0.1:7080/v1/version | jq
# { "version": "0.1.0", "opencode_version": "1.15.13" }

# Round-trip a chat message. The (surface, surface_id, agent) triple
# scopes the opencode session: subsequent calls with the same triple
# reuse the same session.
curl -s -X POST http://127.0.0.1:7080/v1/chat \
  -H 'content-type: application/json' \
  -d '{
        "message": "reply with the single word: pong",
        "channel": {
          "surface": "cli",
          "surface_id": "smoke-test",
          "agent": "build"
        }
      }' | jq
# {
#   "reply": "pong",
#   "oc_session_id": "ses_...",
#   "oc_message_id": "msg_...",
#   "tokens": { "input": 4096, "output": 268 },
#   "cost": 0.0
# }
```

The first POST against a new channel creates an opencode session
(15 s+ on a cold model). Subsequent POSTs to the same channel reuse
it and complete much faster.

## Running integration tests

The default `pytest` run is **offline-only** — it doesn't touch
`opencode` or `ollama`. The single integration test in
`tests/integration/test_chat_real.py` exercises the full
`/v1/chat` round-trip against live services and is opt-in.

### Prerequisites

- `ollama serve` running with `qwen3:latest` pulled
  (`ollama pull qwen3:latest`).
- `opencode serve` running on `OPENCODE_URL` (default
  `http://127.0.0.1:4096`) with at least `ollama` listed as a
  connected provider in `GET /provider`.

If either prerequisite is missing the test self-skips — the suite
won't fail.

### Invocation

```bash
uv run pytest -m integration
```

Expected runtime: ~20–30 s end-to-end on a warm `ollama` cache; the
first run after `ollama serve` boots can take longer while the model
is loaded into memory. The test ceiling is 90 s — anything past that
fails the test.

To exclude integration tests explicitly (CI default):

```bash
uv run pytest -m "not integration"
```

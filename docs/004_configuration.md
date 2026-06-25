# 004 — Configuration

Everything scufris-server reads at startup, where it reads it from,
and what it controls.

There are two configuration mechanisms:

1. **Environment variables** — drive `Settings` (`config.py`). One
   source of truth for runtime / deployment knobs: bind, port, paths,
   upstream URL, override.
2. **`config.toml`** — drives identity (`identity.py`). One source of
   truth for the *user identity* layer: who the user is, which
   surface_ids belong to them.

The split is intentional. v1 collapsed both into `config.toml`; v2
keeps them separate so deployment-flavour knobs (which port, where
the DB lives) can be set per-environment via env vars without
editing the user's per-machine identity file.

## Environment variables

Defined as fields on `Settings` (`config.py:50`). Every field has a
`validation_alias` pointing at its uppercase env var.

Inspect the live values at runtime:

```python
from scufris_server.config import get_settings
get_settings().model_dump()
```

### Reference table

| Variable                    | Default                          | Type   | Notes |
|-----------------------------|----------------------------------|--------|-------|
| `SCUFRIS_BIND`              | `127.0.0.1`                      | str    | Interface scufris-server binds to. Loopback by default; set to `0.0.0.0` for LAN access (then set `SCUFRIS_TOKEN` once `#14` lands). |
| `SCUFRIS_PORT`              | `7080`                           | int    | TCP port. Validated 1–65535. |
| `SCUFRIS_STATE_DIR`         | `$XDG_STATE_HOME/scufris`        | path   | Directory for SQLite + future state. The DB is `scufris.sqlite` inside this dir. Falls back to `~/.local/state/scufris` when `XDG_STATE_HOME` is unset. |
| `SCUFRIS_CONFIG`            | _(unset)_                        | path   | Explicit `config.toml` location. When unset, the loader falls back to `$XDG_CONFIG_HOME/scufris/config.toml`, then `~/.config/scufris/config.toml`. |
| `SCUFRIS_USER_ID`           | _(unset)_                        | int    | Identity override. Pins every `resolve_user` call to this `users.id`. Validated at boot — a stale id crashes startup. See [`003_identity_and_users.md`](003_identity_and_users.md). |
| `OPENCODE_URL`              | `http://127.0.0.1:4096`          | str    | Where `opencode serve` is reachable. Loopback by default. |
| `OPENCODE_SERVER_PASSWORD`  | _(unset)_                        | str    | Sent as HTTP basic-auth password (username empty) per opencode's plugin/serve contract. Optional for loopback; warned about at startup if unset for a remote URL. |
| `OPENCODE_MODEL`            | _(unset)_                        | str    | Override the default model probe. Set to `providerID/modelID` (e.g. `"ollama/qwen3:latest"`) or just a model ID (defaults to `ollama` provider). When unset, the server probes `GET /provider` on startup. |

### Variables that XDG honours

`config.py` and `identity.py` each call `os.environ.get(...)` for
two XDG variables:

| Variable | Used by | Effect |
|----------|---------|--------|
| `XDG_STATE_HOME` | `config._default_state_dir()` | Base for `SCUFRIS_STATE_DIR` default. Spec-compliant fallback to `~/.local/state` when unset. |
| `XDG_CONFIG_HOME` | `identity._xdg_config_path()` | Base for `config.toml` default. Spec-compliant fallback to `~/.config` when unset. |

`SCUFRIS_*` and `OPENCODE_*` env vars override XDG defaults — e.g.
`SCUFRIS_STATE_DIR=/srv/scufris` ignores `XDG_STATE_HOME` entirely.

### What the defaults look like in practice

For `alex` on a typical Linux box with no XDG overrides:

- `SCUFRIS_STATE_DIR` → `/home/alex/.local/state/scufris`
- DB at `/home/alex/.local/state/scufris/scufris.sqlite`
- `SCUFRIS_CONFIG` resolution → `/home/alex/.config/scufris/config.toml`
- `SCUFRIS_BIND:SCUFRIS_PORT` → `127.0.0.1:7080`
- `OPENCODE_URL` → `http://127.0.0.1:4096`

### How `Settings` is accessed

```python
from scufris_server.config import get_settings

settings = get_settings()  # cached
print(settings.bind, settings.port)
```

`get_settings()` is `@lru_cache`d. First call instantiates `Settings()`,
which reads the environment. Subsequent calls return the same
instance. Tests that mutate env vars must call
`get_settings.cache_clear()` to force a re-read.

The lifespan pulls `settings` once and stores it on `app.state.settings`,
so handlers don't normally call `get_settings()` themselves —
they get the per-app settings via `request.app.state.settings`.
This matters for tests, which call
`create_app(settings=Settings(state_dir=tmp_path, ...))` to inject a
test-isolated config.

### The unsafe-settings warning

`config.py:130` defines `warn_unsafe_settings()`. Today it warns when
`OPENCODE_SERVER_PASSWORD` is unset *and* `OPENCODE_URL` is
non-loopback — i.e. unauthenticated requests would fly across the
network. Currently not called from the lifespan; it's available for
the eventual production-deploy CLI tool to invoke.

## `config.toml`: the user identity file

Single TOML file, single user (today). Owned by the user, not the
deployment.

### Location resolution

The lifespan calls `load_user_identity(settings.config_path)`
(`identity.py:152`). The path argument resolves like this, in order:

1. `Settings.config_path` is non-`None` (i.e. `SCUFRIS_CONFIG` was
   set in the environment): use that path verbatim.
2. Otherwise `_xdg_config_path()` is consulted:
   - `XDG_CONFIG_HOME` set in the environment → use
     `$XDG_CONFIG_HOME/scufris/config.toml`.
   - `XDG_CONFIG_HOME` unset → use `~/.config/scufris/config.toml`.

A non-existent file is fine: `load_user_identity()` returns an empty
`IdentityFile(user=None)` and logs at INFO. All requests then fall
through to the default user.

### Schema

```toml
# Required: the human's display name.
[user]
username = "alex"

# Optional: surface name → surface_id.
[user.identity]
cli      = "alex"
telegram = "8231376426"
web      = "alex@example.com"   # works with any string surface name
```

| Section | Field | Type | Required | Meaning |
|---------|-------|------|----------|---------|
| `[user]` | `username` | `string` | yes | Inserted as `users.username`. Must be unique. |
| `[user]` | `identity` | `dict[str, str]` | no | Surface name → surface_id mapping. Default `{}`. |

Everything else — `[server]`, `[scheduler]`, `[user.schedule]`,
`[user.rag]`, `[user.journal]`, `[user.notifications]` — is silently
ignored. v1 used those tables for other features; v2 picks them up
in future tasks. The lifespan logs an INFO line listing top-level
keys it dropped:

```
INFO ignoring top-level keys in /home/alex/.config/scufris/config.toml: ['scheduler', 'server']
```

### Worked example

A minimal valid `config.toml`:

```toml
[user]
username = "alex"

[user.identity]
cli      = "alex"
telegram = "8231376426"
```

Boot logs:

```
INFO loaded config.toml: user=alex
```

After the first call from each surface:

```
INFO TOML resolve: user_id=2 (alex) for (cli, alex)
INFO TOML resolve: user_id=2 (alex) for (telegram, 8231376426)
```

After that, steady state is silent (cache hits log at DEBUG).

### Editing the file

Edits don't take effect until the server restarts.
`load_user_identity()` runs once in the lifespan; there's no
SIGHUP / reload mechanism. (This was a deliberate choice from v1,
preserved verbatim — the design doc §11 calls it out.)

Existing `surface_bindings` rows are sticky — see `003_identity_and_users.md`
"Step 2". To re-route a surface_id after editing TOML, delete the
binding row by hand:

```bash
uv run --active python - <<'PY'
import sqlite3
conn = sqlite3.connect("/home/alex/.local/state/scufris/scufris.sqlite")
conn.execute("DELETE FROM surface_bindings WHERE surface=? AND surface_id=?",
             ("cli", "alex"))
conn.commit()
PY
```

## Other config files (per-host vs per-repo)

The full picture from design doc §12:

| File                                       | Owner    | Purpose                                                |
|--------------------------------------------|----------|--------------------------------------------------------|
| `~/.config/scufris/config.toml`            | user     | Identity (`#12`); future scheduler/journal/RAG sources |
| `$XDG_STATE_HOME/scufris/scufris.sqlite`   | server   | Identity registry, session links, facts, audit log     |
| `<project>/opencode.json`                  | repo     | Providers, default agent, default perms, MCP servers   |
| `<project>/.opencode/agent/*.md`           | repo     | Per-agent prompts/models/permissions                   |
| `<project>/.opencode/tool/*.ts`            | repo     | Custom tools                                           |
| `<project>/.opencode/plugin/scufris.ts`    | repo     | Hooks, fact-injection, custom MCP wiring               |
| `/run/secrets/scufris.env` (NixOS)         | sops     | `OPENCODE_SERVER_PASSWORD`, `SCUFRIS_TOKEN`, etc.      |

- The `~/.config/scufris/config.toml` is the *only* one scufris-server
  reads today. The others are opencode's domain (read by `opencode
  serve` from the project directory) or future-task territory.
- The state DB is created on first boot if missing — see
  [`005_data_model.md`](005_data_model.md).

## Recipes

### Local dev (default everything)

```bash
opencode serve &
uv run python -m scufris_server
```

Picks up `~/.config/scufris/config.toml` if present, else everyone
falls through to user 1.

### Local dev with an explicit config.toml in the project root

```bash
export SCUFRIS_CONFIG="$PWD/config.toml"
uv run python -m scufris_server
```

Useful when iterating on the TOML schema or running multiple scufris
instances against different user identities.

### Local dev with a clean state dir

```bash
export SCUFRIS_STATE_DIR=/tmp/scufris-test
rm -rf $SCUFRIS_STATE_DIR
uv run python -m scufris_server
```

Migrations rerun, default user reseeded, no leftover bindings. Good
for reproducing identity-resolution edge cases.

### Single-user override (dev / smoke test)

```bash
export SCUFRIS_USER_ID=2
uv run python -m scufris_server
```

Pins every request to user 2 regardless of `(surface, surface_id)`.
The lifespan validates 2 exists in `users` at boot — make sure you
seed it via TOML *or* manually before setting the override.

### Listening on the LAN (don't do this without auth)

```bash
export SCUFRIS_BIND=0.0.0.0
uv run python -m scufris_server
```

Today this is **unauthenticated** — anyone on the network can hit
`/v1/chat`. `#14` will add `SCUFRIS_TOKEN` bearer auth before this
becomes a sane configuration to ship.

### Talking to a remote opencode

```bash
export OPENCODE_URL=https://opencode.example.com
export OPENCODE_SERVER_PASSWORD=...
uv run python -m scufris_server
```

If you set the URL but not the password, `warn_unsafe_settings()`
will emit a `UserWarning` at startup.

## Common pitfalls

- **TOML edit not picked up.** Restart the server. There is no
  reload-on-change.
- **`SCUFRIS_USER_ID=999` and the server refuses to start.** That id
  doesn't exist in `users`. Either set `SCUFRIS_USER_ID=1` (default
  user, always present), pre-create the row via `config.toml` and
  one chat call, or remove the override.
- **DB locked / migration errors.** Two scufris-server processes
  pointing at the same `SCUFRIS_STATE_DIR`. The WAL setup is
  multi-process-safe in steady state, but two boots running
  migrations at the same time is racy. Pick a different `state_dir`
  or shut the other one down.
- **Default model missing → 503.** `app.state.opencode_default_model`
  is `None`. Either (a) opencode wasn't reachable at boot — check
  `/v1/healthz`, fix the upstream, restart; or (b) opencode is
  reachable but no connected provider has a default model — check
  `opencode.json` and your provider configuration; or (c) pin the
  model explicitly with `OPENCODE_MODEL=provider/model` to bypass
  the probe entirely.
- **Logs show `"no config.toml at <path>"` when you have one.** The
  path being logged is the one the loader *actually* used. Compare
  it to `$SCUFRIS_CONFIG`, `$XDG_CONFIG_HOME/scufris/config.toml`,
  and `~/.config/scufris/config.toml`. Resolution order is in
  "Location resolution" above.

## Cross-references

- The `Settings` class: `scufris_server/config.py:50`.
- TOML loader: `scufris_server/identity.py:152`.
- Lifespan that consumes them: `scufris_server/app.py:144`.
- The identity resolver: [`003_identity_and_users.md`](003_identity_and_users.md).
- The DB layout the state dir hosts: [`005_data_model.md`](005_data_model.md).

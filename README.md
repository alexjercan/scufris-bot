# Scufris

[![CI](https://github.com/alexjercan/scufris-bot/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/alexjercan/scufris-bot/actions/workflows/ci.yml)

Scuffed Jarvis — a personal assistant bot powered by an Ollama-backed
LangChain agent hierarchy, exposed both as a Telegram bot and a local CLI.

## Nix

The project ships a flake (`uv2nix` + `flake-parts`). All commands assume
flakes are enabled.

```bash
# Build the HTTP daemon → ./result/bin/scufris-server
nix build .#scufris-server

# Build the REPL client → ./result/bin/scufris-cli
nix build .#scufris-cli

# Run the CLI directly (opens the REPL; needs a running scufris-server)
nix run .#scufris-cli

# Dev shell with python + ruff + mypy + pytest on PATH
nix develop

# Sandboxed ruff + mypy + pytest (the same checks CI runs)
nix flake check
```

`packages.default` and `apps.default` both point at `scufris-server`.

### NixOS service

The flake also exports a NixOS module (`nixosModules.scufris`, also
re-exported as `nixosModules.default`) that runs `scufris-server` as a
hardened systemd unit.

```nix
{
  inputs.scufris.url = "github:alexjercan/scufris-bot";

  outputs = {self, nixpkgs, scufris}: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        scufris.nixosModules.default
        {
          services.scufris = {
            enable = true;
            settings = {
              server.bind = "127.0.0.1";
              server.port = 8765;
              ollama.base_url = "http://127.0.0.1:11434";
              ollama.model = "qwen3:latest";
            };
            environmentFile = "/run/secrets/scufris.env";  # optional; SCUFRIS_TOKEN, TELEGRAM_BOT_TOKEN
          };
        }
      ];
    };
  };
}
```

The unit runs under `DynamicUser` with `ProtectSystem=strict`,
`NoNewPrivileges`, a tight syscall filter, restricted address families
(`AF_INET`/`AF_INET6`/`AF_UNIX`) and an empty capability set —
`systemd-analyze security scufris` reports an exposure score below
`2.0`. `MemoryDenyWriteExecute` is **off** by default because some
Python ML libraries need W+X pages; flip
`services.scufris.memoryDenyWriteExecute = true` after verifying with
your real model backend.

`SIGTERM` triggers a graceful drain (default 30s, tunable via
`settings.server.shutdown_grace`); `TimeoutStopSec` is set to `35s` to
match.

The full config schema lives in `utils/config.py`; the module's
`settings` is rendered verbatim to `/etc/scufris/config.toml` via
`pkgs.formats.toml`. Secrets (`SCUFRIS_TOKEN`, `TELEGRAM_BOT_TOKEN`) go
in `environmentFile` — env vars override matching TOML keys at load
time so secrets stay out of the Nix store. See
[`tasks/20260510-192923/DESIGN.md`](tasks/20260510-192923/DESIGN.md) for
the plain env-file recipe (recommended) and sops-nix/agenix/systemd-creds
opt-in patterns.

The flake's `checks.<system>.scufris-vm` boots a NixOS VM, enables the
service, hits `/v1/healthz`, asserts the security score budget, and
verifies clean restart.

### Home Manager

For personal laptops or any setup without root, use the Home Manager
module (`homeManagerModules.scufris`, also exported as `default`). It
installs `scufris-cli` and can optionally run `scufris-server` as a
`systemd --user` unit.

```nix
{
  imports = [scufris.homeManagerModules.default];

  programs.scufris = {
    enable = true;

    settings = {
      user.username = "alex";
      user.identity.cli = "alex";
      ollama.model = "qwen3:latest";
      ollama.base_url = "http://127.0.0.1:11434";
      client.server_url = "http://127.0.0.1:8765";
    };

    # Shared by the user-level server, bot, and any future per-user
    # units. The CLI does *not* auto-source this — see
    # tasks/20260510-192923/DESIGN.md.
    environmentFile = "${config.home.homeDirectory}/.config/scufris/env";

    # Optional: also run the daemon and/or Telegram bot as user services.
    server.enable = true;
    bot.enable = true;
  };
}
```

The module renders `settings` to `${"$XDG_CONFIG_HOME"}/scufris/config.toml`
and exports `SCUFRIS_CONFIG` as a session variable so the CLI, the
optional user-level server, and the optional Telegram bot all read the
same file. Secrets (`SCUFRIS_TOKEN`, `TELEGRAM_BOT_TOKEN`) go in the
top-level `environmentFile` — env vars override TOML keys at load time.
See
[`tasks/20260510-192923/DESIGN.md`](tasks/20260510-192923/DESIGN.md)
for deployment patterns (plain env-file, sops-nix, agenix,
systemd-creds). Note that `home.sessionVariables` only takes effect
for **new** shells — re-source or log out/in after the first switch.

When both `server.enable` and `bot.enable` are true, the bot unit is
ordered `After=scufris.service` so the daemon comes up first; the bot
itself fails fast on an unreachable server and `Restart=on-failure`
handles the startup race.

`systemctl --user status scufris` / `journalctl --user -u scufris`
inspect the unit. The user-level unit applies the subset of systemd
hardening that works without root (`PrivateTmp`, `ProtectSystem=strict`,
`NoNewPrivileges`, `LockPersonality`, ...).

If you also enable the system-wide NixOS module on the same host,
**pick one** — both default to port 8765 and will collide.

The legacy Telegram-bot HM module is still available as
`homeManagerModules.scufris-bot` (unchanged options under
`services.scufris-bot`).

## Running

### Architecture in one paragraph

The agent runtime — model, tools, history, telemetry — runs as a single
long-lived daemon: `scufris-server`. Every front-end (`scufris-cli`,
`scufris-bot`, future TUI/web) is a thin HTTP client of that daemon
sharing the same per-user state. Restarting a front-end never evicts
your conversation, and CLI/Telegram users with the same `user_id` see
the same history.

### Start the server first

```bash
uv run scufris-server      # listens on 127.0.0.1:8765 by default
```

Configuration is via the same `.env` the bot used to read directly
(`OLLAMA_MODEL`, `OLLAMA_BASE_URL`, etc.). Set `SCUFRIS_TOKEN` if you
want bearer-token auth (clients must then pass the same value).

### Telegram bot

Requires the daemon to already be running. Set `TELEGRAM_BOT_TOKEN` and
`ALLOWED_USER_IDS` in the environment (or a `.env` file). If your
server is on another host or uses auth, also set `SCUFRIS_SERVER_URL`
and `SCUFRIS_TOKEN`.

```bash
uv run scufris-bot
```

The bot fails fast at startup if `scufris-server` is unreachable or
auth fails — there's no useful partial state. Each Telegram user's
numeric id is forwarded to the server as their `user_id`, so per-user
history is preserved across bot restarts (and shareable with
`scufris-cli` if you set `SCUFRIS_USER_ID` to your Telegram id).

While the agent is thinking, the bot posts a placeholder message and
edits it in place with a depth-aware "tool calls / sub-agents asked"
trail (rate-limited to ~1 edit/sec). The placeholder is deleted when
the final answer arrives so your scrollback only retains the answer
itself.

## Config file

Both front-ends and the server read a single TOML config file. Lookup
order (first hit wins, missing file is OK):

1. `$SCUFRIS_CONFIG`
2. `$XDG_CONFIG_HOME/scufris/config.toml`
3. `~/.config/scufris/config.toml`

The full schema lives in `utils/config.py`. Sections: `[user]`,
`[user.identity]`, `[user.journal]`, `[telegram]`, `[ollama]`,
`[history]`, `[server]`, `[client]`. A useful starting shape:

```toml
[user]
username = "alex"          # canonical name; hashed into the wire user_id
timezone = "Europe/Berlin"

[user.identity]
# surface_id values that should resolve to the same user_id as `username`.
# Telegram ids are bare integers; the CLI key is whatever
# `getpass.getuser()` returns (or `$SCUFRIS_USER`).
telegram = 8231376426
cli      = "alex"

[ollama]
model    = "qwen3:latest"
base_url = "http://127.0.0.1:11434"

[server]
bind = "127.0.0.1"
port = 8765
# token = "..."            # SECRET — leave in env (SCUFRIS_TOKEN)

[client]
server_url = "http://127.0.0.1:8765"

[telegram]
allowed_user_ids = [8231376426]
# bot_token = "..."        # SECRET — leave in env (TELEGRAM_BOT_TOKEN)
```

Environment variables override matching TOML keys at load time —
secrets (`SCUFRIS_TOKEN`, `TELEGRAM_BOT_TOKEN`) belong in env, the
rest belongs in the file. The full env→TOML map is documented in the
module docstring of `utils/config.py`.

When a surface_id matches an `[user.identity]` binding,
`POST /v1/identity/resolve` returns the same `user_id` for both `cli`
and `telegram` — so `/clear` from one surface clears history on the
other. Without a config the bot falls back to passing the raw Telegram
numeric id and the CLI hashes `getpass.getuser()`, exactly like the
pre-config behavior.

## Debugging

For local development you usually don't want to round-trip through Telegram
on every change. There's a REPL-style CLI that talks to the same agent
pipeline directly from your terminal.

### Starting the CLI

```bash
uv run scufris-cli
```

No Telegram credentials are needed — `load_config(require_telegram=False)`
skips that validation. You still need a working Ollama setup (configured
via `OLLAMA_MODEL`, `OLLAMA_BASE_URL`, etc., same as the bot).

### Using it

You get a prompt with line editing, arrow-key history (persisted to
`~/.scufris_cli_history`), and Ctrl-R search via `readline`. Assistant
replies are rendered as Markdown inside a green panel.

```
Scufris CLI — type /help for commands, Ctrl-D on empty line to exit.
> what's 2 + 2?
╭─ scufris ──────────────────────────────────────────────╮
│ 4                                                      │
╰────────────────────────────────────────────────────────╯
> /stats
Per-agent:
  agent       model         memory  calls  last
  ──────────  ────────────  ──────  ─────  ────
  scufris     qwen3:latest  2 msgs      1  0s ago
> /exit
bye!
```

### Slash commands

| Command      | What it does                                                |
| ------------ | ----------------------------------------------------------- |
| `/help`      | List available commands                                     |
| `/clear`     | Clear chat history for this session                         |
| `/stats`     | Per-agent memory + telemetry breakdown                      |
| `/multiline` | Toggle multiline input (end with a single `.` line)         |
| `/exit`, `/quit` | Exit the REPL (Ctrl-D on an empty line works too)       |

Anything that doesn't start with `/` is sent to the agent. Tool activity
is logged to stderr through the normal `scufris-bot.agent.tools` logger,
so you can watch what the agent is doing in real time.

### Multiline input

Toggle with `/multiline`, then type as many lines as you want and finish
with a line containing a single `.`:

```
> /multiline
multiline mode on — finish input with a single '.' line
> please summarize the following:
… line one
… line two
… .
```

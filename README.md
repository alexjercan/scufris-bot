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
            bind = "127.0.0.1";
            port = 8765;
            ollamaUrl = "http://127.0.0.1:11434";
            model = "qwen3:latest";
            environmentFile = "/run/secrets/scufris.env";  # optional
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
`SCUFRIS_SHUTDOWN_GRACE`); `TimeoutStopSec` is set to `35s` to match.

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

    # Optional: also run the daemon as a user service.
    server = {
      enable = true;
      bind = "127.0.0.1";
      port = 8765;
      model = "qwen3:latest";
      ollamaUrl = "http://127.0.0.1:11434";
      environmentFile = "${config.home.homeDirectory}/.config/scufris/env";
    };
  };
}
```

When `server.enable = true`, `SCUFRIS_SERVER_URL` is auto-injected into
`home.sessionVariables` so a fresh shell's `scufris-cli` connects to
the user daemon with no extra setup. Add `SCUFRIS_TOKEN` (or anything
else) via `programs.scufris.clientEnvironment`. Note that
`home.sessionVariables` only takes effect for **new** shells — re-source
or log out/in after the first switch.

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

### Telegram bot

Requires `TELEGRAM_BOT_TOKEN` and `ALLOWED_USER_IDS` in the environment
(or a `.env` file).

```bash
uv run scufris-bot
```

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

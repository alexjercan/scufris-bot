# NixOS module + hardened systemd unit for scufris-server

- STATUS: OPEN
- PRIORITY: 65
- TAGS: deploy,nix,systemd

## Goal

Ship a NixOS module (`nixosModules.scufris`) so a system can install the
server with `services.scufris.enable = true;`. The module wires a hardened
systemd unit, journald logging, and a clean upgrade story.

## Scope

### In
- `nix/modules/scufris.nix` exporting `services.scufris` options:
  - `enable` (bool)
  - `package` (default `pkgs.scufris-server` from this flake's overlay)
  - `bind` (default `127.0.0.1`)
  - `port` (default `8765`)
  - `model` (string, passes through `SCUFRIS_MODEL`)
  - `ollamaUrl` (string, `SCUFRIS_OLLAMA_URL`)
  - `environmentFile` (path, sourced for secrets — token, etc.)
  - `extraEnvironment` (attrset → `Environment=` lines)
  - `openFirewall` (bool, default false)
  - `user` / `group` (defaults: `DynamicUser = true`)
- Generated systemd unit:
  - `Type = "notify"` if server implements sd_notify, else `"simple"`.
  - `ExecStart = "${cfg.package}/bin/scufris-server"`.
  - Hardening: `DynamicUser`, `ProtectSystem=strict`,
    `ProtectHome=true`, `PrivateTmp=true`, `PrivateDevices=true`,
    `NoNewPrivileges=true`, `RestrictNamespaces=true`,
    `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`,
    `SystemCallFilter=@system-service`, `LockPersonality=true`,
    `MemoryDenyWriteExecute=true` (only if it doesn't break Python
    JIT-ish deps; verify), `CapabilityBoundingSet=`.
  - `StateDirectory=scufris` for any future on-disk history.
  - `Restart=on-failure`, `RestartSec=5`.
  - `TimeoutStopSec=35` (matches server's 30s grace).
  - Optional socket activation via `systemd.sockets.scufris` if `bind`
    is a unix path or for fast restarts; default off.
- Flake `nixosModules.default = nixosModules.scufris`.
- Flake `checks.<system>.scufris-vm` — `nixosTest` that boots a VM,
  enables the service, hits `GET /v1/healthz`, sends one chat request
  with a stub model (mock backend), asserts 200 + restart survives.
- Docs: short section in README on enabling the service.

### Out
- Home Manager equivalent (separate task).
- Secrets management beyond `environmentFile` (separate task: sops/agenix).
- Reverse proxy / TLS configuration (out of scope — recommend nginx
  module separately).

## Acceptance criteria

- A flake consumer can do:
  ```nix
  imports = [ scufris.nixosModules.default ];
  services.scufris = { enable = true; environmentFile = "/run/secrets/scufris"; };
  ```
  and `nixos-rebuild switch` brings up a working `scufris-server`.
- `systemctl status scufris` shows active, journald has structured logs.
- `curl http://127.0.0.1:8765/v1/healthz` returns 200.
- `systemd-analyze security scufris` score ≤ 2.0 ("OK" or better).
- `nix flake check` runs the VM test green.
- Killing the unit with SIGTERM lets in-flight requests finish (≤ 30s)
  before exit; verified in the VM test.

## Notes

- `DynamicUser` means no persistent UID — `StateDirectory` handles file
  ownership across upgrades. Fine for v1 (no persistence) and forward-
  compatible.
- `MemoryDenyWriteExecute` can break ctypes / some ML libs. Test with
  the real model backend before enabling; otherwise leave off and
  document.
- If the server talks to a remote Ollama, no special hardening tweak
  needed; if local Ollama on same host, `127.0.0.1` is allowed by the
  AF restriction.
- Avoid `User=scufris` static user unless persistence forces it — keeps
  module zero-config.

## References

- `tasks/20260510-192716/TASK.md` — package this module consumes.
- `tasks/20260510-192505/TASK.md` — server's SIGTERM behavior + env vars.
- NixOS systemd hardening cheat sheet:
  https://nixos.wiki/wiki/Systemd_hardening (and `systemd-analyze
  security` upstream docs).
- `nixos/tests` examples in nixpkgs for reference VM-test structure.

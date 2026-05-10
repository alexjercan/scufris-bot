# Home Manager module for user-level scufris install

- STATUS: OPEN
- PRIORITY: 60
- TAGS: deploy,nix,home-manager

## Goal

Provide `homeManagerModules.scufris` so a user can install `scufris-cli`
into their profile and optionally run `scufris-server` as a
`systemd.user.service` — no root required, ideal for personal laptops
and dev machines.

## Scope

### In
- `nix/hm-modules/scufris.nix` exporting `programs.scufris` options:
  - `enable` (bool) — installs `scufris-cli` into the user profile.
  - `package` (default this flake's `scufris-cli`).
  - `server.enable` (bool) — also installs `scufris-server` and a
    `systemd.user.services.scufris` unit.
  - `server.bind`, `server.port`, `server.model`, `server.ollamaUrl`,
    `server.environmentFile`, `server.extraEnvironment` — mirror the
    NixOS module options.
  - `clientEnvironment` — sets `SCUFRIS_SERVER_URL` and `SCUFRIS_TOKEN`
    in the user's session via `home.sessionVariables`.
- Generated `systemd.user.services.scufris`:
  - `ExecStart`, `Restart=on-failure`, `RestartSec=5`,
    `TimeoutStopSec=35`.
  - User-level hardening where supported (`PrivateTmp`,
    `ProtectSystem=strict`, `NoNewPrivileges`); skip the system-only
    options (DynamicUser, capability bounding) that don't apply to
    user units.
  - `WantedBy = [ "default.target" ]`.
- Flake `homeManagerModules.default = homeManagerModules.scufris`.
- Docs: README section showing a minimal `home.nix` snippet.

### Out
- NixOS module (separate task, already covered).
- macOS launchd unit (could mirror the user service later if demand exists).
- Multi-user shared daemon on a workstation (use the NixOS module instead).

## Acceptance criteria

- A flake consumer can do:
  ```nix
  imports = [ scufris.homeManagerModules.default ];
  programs.scufris = {
    enable = true;
    server.enable = true;
    server.environmentFile = "${config.home.homeDirectory}/.config/scufris/env";
  };
  ```
  and `home-manager switch` installs `scufris-cli` plus a running
  `systemctl --user status scufris` daemon.
- `scufris-cli` in a fresh shell connects to the user daemon without
  manual env tweaking (because `home.sessionVariables` set
  `SCUFRIS_SERVER_URL`).
- Disabling `server.enable` removes the unit cleanly on next switch.
- `journalctl --user -u scufris` shows logs.

## Notes

- Keep option names aligned 1:1 with the NixOS module so users can move
  between system-wide and user-level installs without relearning.
- `home.sessionVariables` only affects new shells — document this; for
  immediate effect users must re-source.
- If the user daemon is enabled on a system that also has the NixOS
  module enabled, document the port-collision footgun and recommend
  picking one.

## References

- `tasks/20260510-192748/TASK.md` — NixOS module (mirror its option set).
- `tasks/20260510-192636/TASK.md` — CLI's env var contract
  (`SCUFRIS_SERVER_URL`, `SCUFRIS_TOKEN`, `SCUFRIS_USER`).
- Home Manager systemd.user.services docs:
  https://nix-community.github.io/home-manager/options.xhtml

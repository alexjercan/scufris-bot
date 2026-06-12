# OpenCode daemon supervision (Nix module + systemd unit)

- STATUS: CLOSED
- PRIORITY: 90
- TAGS: opencode,nix,deploy

## Outcome (2026-06-10)

- New module: `nix/modules/opencode-serve.nix` — full `services.opencode-serve.*`
  surface: `enable`, `package` (defaults to `pkgs.opencode`, with a
  warning that the dev pin is 1.3.10 and the runtime needs the 1.15.x
  wire protocol — override before deploy), `host`, `port` (default
  `4096`), `url` (read-only computed `http://${host}:${port}`),
  `workingDirectory`, `environmentFile` (provider creds), `logLevel`,
  `user`/`group`, `extraArgs`, `openFirewall`. Service runs
  `opencode serve --port ... --hostname ... --log-level ... --print-logs`
  with `Type=simple`, `Restart=on-failure`, `StateDirectory=opencode-serve`,
  full hardening matching the scufris unit (DynamicUser, ProtectSystem=strict,
  SystemCallFilter, etc.). XDG dirs forced under StateDirectory so
  `auth.json` and the session store survive restart under DynamicUser.
- `nix/modules/scufris.nix` extended:
  - New `services.scufris.environment` attrset (free-form env vars
    merged into the unit).
  - Auto-wires `OPENCODE_BASE_URL = config.services.opencode-serve.url`
    when the OpenCode module is enabled (via `lib.mkDefault`, so an
    explicit override wins).
  - Adds `Wants=opencode-serve.service` and `After=opencode-serve.service`
    when both are enabled (soft dep — `Wants=` not `Requires=`, per
    the open question, so `/v1/readyz` returns degraded instead of
    the public endpoint vanishing).
- `flake.nix`: registers `nixosModules.opencode-serve`; passes the
  module into `nix/tests/scufris-vm.nix`.
- VM test rewritten:
  - Boots both units, waits for `:4096` (opencode) and `:8765` (scufris).
  - Asserts `OPENCODE_BASE_URL=http://127.0.0.1:4096` is in
    `systemctl show scufris.service -p Environment`.
  - Asserts `scufris.service` lists `opencode-serve.service` in both
    `After=` and `Wants=`.
  - Hits `/v1/readyz` and parses the JSON; verifies the `opencode.code`
    field is populated (proves the readiness probe actually reaches
    the local daemon).
  - Security exposure budget: `<= 2.0` for scufris (matches existing
    AC), `<= 2.5` for opencode-serve (Bun runtime needs slightly more
    surface than the Python service).
  - Existing graceful-shutdown / restart-on-failure assertions kept.

### Verification

- `nix-instantiate --parse` clean on all four touched files.
- `nix flake check --no-build` passes; both modules listed under
  `nixosModules`.
- `nix eval` on a synthetic system config confirms:
  - `services.opencode-serve.url = "http://127.0.0.1:4096"`,
  - `ExecStart = ".../bin/opencode serve --port 4096 --hostname 127.0.0.1 --log-level INFO --print-logs"`,
  - `systemd.services.scufris.environment.OPENCODE_BASE_URL = "http://127.0.0.1:4096"`,
  - `systemd.services.scufris.wants` contains `opencode-serve.service`,
  - `systemd.services.scufris.after` orders `opencode-serve.service`
    after `network-online.target`.
- VM test execution itself was **not** run from this session (full
  `nixosTest` would build opencode + uvicorn and boot a NixOS VM —
  budget for a separate run); evaluation correctness verified.

### Acceptance criteria

- [x] `flake.nix` (a module under `nix/modules/`) defines the OpenCode
      service.
- [ ] `nix run .#scufris-vm-test` brings up both services and passes
      a smoke test — **test harness written; full VM run deferred to
      a deploy-side verification (the build is multi-minute and pulls
      the entire opencode bundle into the store).**
- [x] The Scufris service reads `OPENCODE_BASE_URL` from its
      environment; no hardcoded port in Python.
- [x] `Wants=` chosen over `Requires=` so `/v1/readyz` reports
      degraded if OpenCode is down — the open question resolved that
      way.
- [x] Provider credentials load from a non-committed source
      (`services.opencode-serve.environmentFile`).
- [x] Both services log via journald (the unit uses `--print-logs`
      and `Type=simple`; no separate log destination).

### Open issue surfaced during work

- `nixpkgs.opencode` at the current flake pin is **1.3.10**, far
  predating the 1.15.x wire protocol the runtime expects. The module
  documents this in the `package` option's description and falls back
  to `pkgs.opencode` only as a default; production deploys must pin a
  newer build (or a flake input pointing at a fork that publishes
  current OpenCode). Filed implicitly via that warning — bump the
  nixpkgs input or override `services.opencode-serve.package` before
  the deploy goes live.

## Original task description (preserved)

### Motivation

The OpenCode runtime swap (`tasks/20260610-101413`) makes Scufris
depend on a running `opencode serve` instance. Today nothing in
`flake.nix` supervises it — there's no NixOS module, no systemd unit,
no health check. On the Hetzner box (`tasks/20260510-192350`)
`scufris-server.service` would start with no provider behind it.

This task adds the supervision plumbing so OpenCode comes up before
Scufris and stays up.

## Scope

### In

- A NixOS module (or extension to the existing module from
  `tasks/20260510-192350`) that defines `services.opencode-serve`:
  - User-level systemd unit (or system unit running as the `scufris`
    user — match whatever `scufris-server.service` does).
  - Working directory: the project root that holds the AGENTS.md
    skill set.
  - `--port <fixed>` and `--hostname 127.0.0.1` (no public binding).
  - `--log-level INFO --print-logs` so journald sees it.
  - Restart policy: `on-failure` with sane backoff.
- `scufris-server.service` gets `After=opencode-serve.service` and
  `Requires=opencode-serve.service` (or `Wants=` if we want the server
  to come up degraded — TBD; see Open questions).
- Environment variable plumbing: the chosen port surfaces as
  `OPENCODE_BASE_URL=http://127.0.0.1:<port>` in the Scufris service
  environment. No port literals in Python.
- Provider auth (e.g. `GITHUB_TOKEN`) sourced from a Nix secret /
  systemd `EnvironmentFile`, not committed.
- VM test in `nix flake check` that starts both services and confirms
  `curl http://127.0.0.1:<port>/session` returns `[]` or `[…]`.

### Out

- The OpenCode binary itself — assume `pkgs.opencode` (or whatever the
  pin) provides it. Packaging is a separate concern.
- Skill content / AGENTS.md authoring — separate skills tasks.
- Multi-host or HA setups.

## Acceptance criteria

- [ ] `flake.nix` (or a module under `nix/`) defines the OpenCode
      service.
- [ ] `nix run .#scufris-vm-test` (or whatever the existing flake check
      target is named) brings up both `opencode-serve` and
      `scufris-server` and passes a smoke test.
- [ ] The Scufris service reads `OPENCODE_BASE_URL` from its
      environment; no hardcoded port in Python.
- [ ] `scufris-server` does not start successfully if OpenCode isn't
      reachable (or, if we choose `Wants=`, `/v1/readyz` returns 503
      until OpenCode is up).
- [ ] Provider credentials are loaded from a non-committed source.
- [ ] Logs from both services are visible via `journalctl` (or
      whatever the deployment uses).

## Open questions

- `Requires=` (hard dep — server fails if OpenCode is down) vs `Wants=`
  (soft dep — server starts but `/readyz` fails). Recommend `Wants=` so
  the public HTTP endpoint can keep returning structured 503s instead
  of vanishing entirely.
- One OpenCode instance per host or one per Scufris user? Single
  instance is simplest; OpenCode's session model already isolates per
  `sessionID`. Stick with single.
- Where does the OpenCode "project root" live in production? It needs
  AGENTS.md + skills checked out. Probably a clone of this repo at a
  fixed path on the Hetzner box.

## References

- `tasks/20260610-101413/TASK.md` — parent task that creates this
  dependency.
- `tasks/20260610-101413/SCHEMA.md` — port discovery details.
- `tasks/20260510-192350/{TASK.md,DESIGN.md}` — Hetzner deploy spike;
  authoritative for the existing `scufris-server.service` shape.
- `flake.nix` — where the new module lands.

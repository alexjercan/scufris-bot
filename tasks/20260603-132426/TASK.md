# Unify utils/config + user_config; flake modules render TOML

- STATUS: CLOSED
- PRIORITY: 75
- TAGS: config,nix,refactor

## Goal

Collapse the two parallel config layers (`utils/config.py` for env-only
runtime knobs and `utils/user_config.py` for the XDG TOML user-identity
file) into a single TOML-first `Config` dataclass, and switch both
flake modules from setting `Environment=` per knob to rendering the
TOML config and pointing `SCUFRIS_CONFIG` at it.

## What changed

### Python

- New `utils/config.py`: frozen `Config` with nested `user`,
  `telegram`, `ollama`, `history`, `server`, `client` sections. Strict
  TOML validation (rejects bool-as-int etc.), warns on unknown keys.
  `parse_config(toml)` + `load_config(*, require_telegram,
  explicit_path, use_dotenv)` are the public surface.
- Env vars are now an *override layer* on top of the TOML, not the
  source of truth. Map documented in the module docstring; secrets
  (`SCUFRIS_TOKEN`, `TELEGRAM_BOT_TOKEN`) and per-invocation knobs
  (`SCUFRIS_USER`, `SCUFRIS_USER_ID`, `SCUFRIS_TELEMETRY`,
  `SCUFRIS_COMPACTOR{,_MODEL}`, `SCUFRIS_CONFIG`) stay env-only.
- Deleted `utils/user_config.py`; `UserIdentity` / `UserJournal` /
  `ResolvedIdentity` / `resolve_user_id` / `config_search_paths` now
  live in `utils/config.py`.
- Migrated all call sites: `utils/agent_builder.py`, `bot.py`,
  `cli.py`, `scufris_server/{__main__,app,auth,bootstrap}.py`,
  `scufris_server/routes/{stats,admin,identity}.py`. `Runtime` no
  longer has a separate `user_config`. `auth.require_token` now reads
  the token off `request.app.state.runtime.config.server.token`
  (was a static env read).
- Tests: `tests/test_user_config.py` rewritten as `tests/test_config.py`
  (27 tests). `tests/test_server.py` builds real `Config` instances via
  a `_stub_config(token=...)` helper instead of stubbing the dataclass.
  `tests/test_sub_agent_memory.py::_make_config` updated for nested
  attrs.

### Nix

- `nix/modules/scufris.nix` (NixOS): rewritten. Single
  `services.scufris.settings` of type `pkgs.formats.toml.type`,
  rendered via `tomlFormat.generate` to `/etc/scufris/config.toml`
  (`environment.etc`). The systemd unit's only Scufris-specific env
  var is `SCUFRIS_CONFIG=/etc/scufris/config.toml`. Secrets still
  come from `environmentFile`. `openFirewall` reads the port out of
  `settings.server.port` (default 8765). Hardening unchanged.
- `nix/hm-modules/scufris.nix` (Home Manager): same shape — single
  `programs.scufris.settings` rendered to
  `${XDG_CONFIG_HOME}/scufris/config.toml` via `xdg.configFile`, plus
  `home.sessionVariables.SCUFRIS_CONFIG`. Optional user systemd unit
  reads the same TOML. Removed the old per-field options
  (`bind`/`port`/`logLevel`/`model`/`ollamaUrl`/`extraEnvironment`)
  and the `clientEnvironment` escape hatch — the file *is* the
  configuration now. Secrets still go through `server.environmentFile`.
- `nix/tests/scufris-vm.nix`: switched to `services.scufris.settings`.
- README "Config file" section + both flake examples updated for the
  new schema and env-override semantics.

## Acceptance

- `nix flake check` passes (ruff, mypy, pytest = 349 tests, NixOS VM
  integration test, both module evaluations).
- Old options (`services.scufris.{bind,port,logLevel,model,ollamaUrl,
  extraEnvironment}` and `programs.scufris.server.{bind,port,...}`,
  `programs.scufris.clientEnvironment`) removed. Migration is to move
  them into `settings.<section>.<key>`. Documented in README.

## Notes / follow-ups

- Pre-existing mypy stub-missing warnings in the dev shell are unchanged;
  `nix flake check` is the source of truth.
- `[user.journal].den_path` is parsed but still not wired into the
  journal tools — pre-existing follow-up.

# Nix flake: package scufris-server and scufris-cli

- STATUS: CLOSED
- PRIORITY: 70
- TAGS: deploy,nix

## Goal

Produce a `flake.nix` that builds scufris-bot as two installable packages
(`scufris-server`, `scufris-cli`), exposes a dev shell matching today's
`nix develop` environment, and runs `nix flake check` for CI.

## Scope

### In
- `flake.nix` with:
  - `inputs`: `nixpkgs` (unstable or 24.05+), `flake-utils`, and either
    `pyproject-nix` or `poetry2nix` (decision in spike below).
  - `packages.<system>.scufris-server` — `python3.withPackages` style or a
    proper `buildPythonApplication` derivation with all deps from
    `pyproject.toml`.
  - `packages.<system>.scufris-cli` — same source, different entry point.
  - `packages.<system>.default = scufris-server`.
  - `apps.<system>.{scufris-server,scufris-cli}` for `nix run`.
  - `devShells.<system>.default` — Python + dev tools (ruff, pytest,
    mypy) + project deps; mirrors current ad-hoc shell.
  - `checks.<system>` — runs ruff, pytest, mypy as flake checks so
    `nix flake check` exercises the full QA gate.
- `pyproject.toml` edits:
  - `[project.scripts]`:
    - `scufris-server = "scufris_server.__main__:main"` (or wherever the
      server entrypoint lands per task 20260510-192505).
    - `scufris-cli = "cli:main"` (rename `cli.py`'s `main` if needed).
  - Confirm all runtime deps are declared (currently the project may rely
    on system Python with manually installed packages — audit and pin).
- Brief `README.md` section on `nix run`, `nix build`, `nix develop`.

### Out
- NixOS module / systemd unit (separate task).
- Home Manager module (separate task).
- Caching / Cachix setup (covered by CI task).
- Cross-compilation, static binaries, container images.

## Acceptance criteria

- `nix build .#scufris-server` produces a runnable binary at
  `result/bin/scufris-server`.
- `nix build .#scufris-cli` produces `result/bin/scufris-cli`.
- `nix run .#scufris-cli` opens the REPL (assuming a server is reachable
  via env defaults).
- `nix develop` drops into a shell where `pytest`, `ruff check .`,
  `mypy .` all work without further setup.
- `nix flake check` runs ruff + pytest + mypy and passes on a clean tree.
- Build is reproducible: two `nix build` runs from the same commit yield
  identical store paths (modulo Python bytecode timestamps if unavoidable).

## Notes

- **pyproject-nix vs poetry2nix decision:** if the project currently uses
  PEP 621 `pyproject.toml` without poetry, prefer `pyproject-nix`
  (lighter, no poetry lock required). If there's a `poetry.lock`, use
  `poetry2nix`. Audit first.
  - NOTE from user: since we are using `uv` and `uv2nix` use those - also we
    are using `flake-parts` instead of `flake-utils`
- Some deps (e.g. `langchain*`, ollama clients) may not be in nixpkgs;
  `pyproject-nix` handles this via `buildPythonPackage` overlays. Budget
  time for 2–3 missing-package overlays.
- `passthru.tests` on the derivation can re-export `checks` for
  downstream consumers (the NixOS module's VM test in the next task).
- Keep `scufris-server` and `scufris-cli` as separate `apps` so users
  who only want the CLI don't pull in server-only deps if they ever
  diverge — today they share everything, but plan for split.

## References

- `pyproject.toml` (current state — needs audit and `[project.scripts]`).
- `tasks/20260510-192505/TASK.md` — defines `scufris_server` module name.
- `tasks/20260510-192636/TASK.md` — defines CLI entrypoint name.
- pyproject-nix: https://github.com/nix-community/pyproject.nix
- poetry2nix: https://github.com/nix-community/poetry2nix

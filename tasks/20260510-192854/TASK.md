# GitHub Actions CI: ruff, pytest, mypy, nix flake check

- STATUS: CLOSED
- PRIORITY: 55
- TAGS: ci

## Goal

Set up GitHub Actions so every push and PR runs the full QA gate (lint,
type-check, tests, Nix build, NixOS VM test) with aggressive caching to
keep wall time low.

## Scope

### In
- `.github/workflows/ci.yml` with jobs:
  1. **lint-and-test** (ubuntu-latest):
     - Checkout, install Nix (DeterminateSystems/nix-installer-action),
       enable magic-nix-cache or cachix.
     - `nix develop --command ruff check .`
     - `nix develop --command mypy .`
     - `nix develop --command pytest -q`
  2. **flake-check** (ubuntu-latest):
     - `nix flake check -L` (runs the checks defined in the flake,
       including the NixOS VM test from task 20260510-192748).
  3. **build** (ubuntu-latest):
     - `nix build .#scufris-server .#scufris-cli`
     - Upload `result/` symlink targets as artifacts (optional).
- Concurrency group keyed on `${{ github.workflow }}-${{ github.ref }}`
  with `cancel-in-progress: true` to drop superseded runs.
- Triggers: `push` to main, `pull_request` to any branch.
- Status badge added to README.
- `.github/dependabot.yml` for `github-actions` ecosystem (weekly).

### Out
- Release / publishing workflow (separate task if/when needed).
- Cachix push (requires secrets; document setup steps but don't enable
  by default — magic-nix-cache is enough for OSS).
- Multi-OS matrix (macOS, Windows). Nix on macOS runners is supported
  but slow; defer until needed.

## Acceptance criteria

- A PR opened against this repo triggers all three jobs.
- Cold cache run completes in ≤ 15 min; warm cache ≤ 5 min.
- Failing ruff / mypy / pytest blocks merge.
- VM test failure blocks merge.
- README shows a green badge for the main branch.

## Notes

- Prefer `DeterminateSystems/nix-installer-action@main` +
  `DeterminateSystems/magic-nix-cache-action@main` — zero-config caching
  for OSS repos, no Cachix account required.
- Run lint/type/test inside `nix develop` rather than installing Python
  manually: keeps CI and local dev in lockstep.
- `nix flake check -L` prints full logs (helps debug VM test failures).
- If the VM test is slow (>5 min), split it into its own job so unit
  tests still feedback fast.
- Don't run `nix flake check` AND the explicit `pytest`/`ruff`/`mypy` if
  the flake checks already cover them — pick one to avoid double work.
  Recommendation: keep both initially (faster feedback from the explicit
  job, completeness from flake check), drop the redundancy once stable.

## References

- `tasks/20260510-192716/TASK.md` — flake (provides `devShell` and
  `checks`).
- `tasks/20260510-192748/TASK.md` — VM test invoked via `flake check`.
- DeterminateSystems actions:
  https://github.com/DeterminateSystems/nix-installer-action
  https://github.com/DeterminateSystems/magic-nix-cache-action

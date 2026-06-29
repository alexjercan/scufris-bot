# Plan for completing scufris-cli v2

This plan outlines the steps to complete the remaining work for the `scufris-cli v2` task, following the user's specified order.

## Phase 1: Documentation & Initial Unit Tests

### 1. Documentation Updates
- [ ] Update `docs/000_overview.md`: Move "scufris-cli (#14)" from "Not implemented yet" to "Implemented today".
- [ ] Update `docs/001_architecture.md`: 
    - Add "Client packages" section explaining the SDK (`scufris_client`) and REPL (`scufris_cli`) split.
    - Update module map with `scufris_client/` and `scufris_cli/`.
- [ ] Update `docs/006_development.md`:
    - Add `scufris_client/` and `scufris_cli/` to the layout block.
    - Update test counts: unit tests (+~32) and integration tests (+1).
    - Mark backlog item #14 as closed.

### 2. Initial Unit Test Expansion
- [ ] Audit `tests/unit/test_scufris_cli.py` for coverage of the renderer (`make_render_thinking`) branches:
    - `tool_call`
    - `tool_result` (success/failure)
    - `tool_meta`
    - `text` (delta)
- [ ] Ensure `_Settings` (full vs short thinking) is tested for truncation.

## Phase 2: Finalizing Step 5 (CLI Renderer)

### 3. Renderer Audit & Verification
- [ ] Review `scufris_cli/__main__.py` to ensure `make_render_thinking` strictly follows D5:
    - No `compaction` branch.
    - No `prior_turns`/`context` conditionals.
    - No `is_sub_agent` verb split.
    - `tool_result` handled as a first-class branch.
- [ ] Verify `_handle_message` correctly uses the renderer and handles error types (Connection, Auth, Server, Cancelled) and the final `Panel(Markdown(...))`.
- [ ] Run `ruff check` and `mypy --strict` on `scufris_cli/`.

## Phase 3: Completing Steps 6 through 9

### 4. Step 6: Slash Command Dispatch & Tests
- [ ] Expand `tests/unit/test_scufris_cli.py` to include full coverage for `/slash` commands:
    - `/help`
    - `/sessions` (success/empty)
    - `/clear` (success/empty)
    - `/thinking` (toggle full/short)
    - `/multiline` (toggle)
    - `/exit` / `/quit`
- [ ] Verify dispatch logic in `_amain`'s REPL loop.
- [ ] Run `ruff check` and `mypy --strict` on `scufris_cli/`.

### 5. Step 7: Integration Testing
- [ ] Create `tests/integration/test_cli_real.py`:
    - Spawn `scufris-server` subprocess.
    - Use `ScufrisClient` to send "reply with 4".
    - Assert `thinking` event and `done` event with "4".
    - Assert `oc_session_id` is present.
    - Ensure cleanup (kill server/clear session).

### 6. Step 9: Pyproject & Close-out
- [ ] Update `pyproject.toml`:
    - Add `scufris_client` and `scufris_cli` to `packages` and `only-include`.
    - Repoint `scufris-cli` script to `"scufris_cli.main:main"`.
- [ ] Final verification:
    - Run all unit and integration tests.
    - Run `ruff` and `mypy --strict` across the entire repo.
- [ ] Mark `tasks/20260613-091049/TASK.md` as `STATUS: CLOSED`.

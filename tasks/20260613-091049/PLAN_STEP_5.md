# Plan for Step 5: CLI Renderer

The objective is to implement the `make_render_thinking` function and integrate it into the REPL loop, adhering to the design decisions in D5.

## Current Status
- [x] `make_render_thinking` function structure is present in `scufris_cli/__main__.py`.
- [x] Integration into the REPL loop in `_amain` is present.
- [x] `_handle_message` is implemented and uses the renderer.
- [ ] **Refinement needed**: The renderer currently still includes the `is_sub_agent` verb distinction ("asks" vs "uses"), which D5 explicitly requires dropping in favor of hardcoding "uses".

## Tasks

### 1. Refine Renderer Implementation
- [ ] Update `make_render_thinking` in `scufris_cli/__main__.py` to remove `_is_sub_agent` and hardcode the verb to `"uses"` for `tool_call` events.
- [ ] Verify that `tool_result` rendering correctly handles the `failed:` prefix for red error styling.
- [ ] Double-check that all `text` and `tool_meta` branches use the `[grey50]` style.
- [ ] Confirm the removal of all `compaction`, `prior_turns`, and `context` conditionals.

### 2. Verification
- [ ] Run `ruff check scufris_cli/` to ensure no linting errors.
- [ ] Run `mypy --strict scufris_cli/` to ensure type safety.

## Next Steps
Once Step 5 is verified, proceed to Step 6: CLI slash dispatch + `/sessions` command and unit tests.

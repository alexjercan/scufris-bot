# Phase 3.6 — Unit tests for history layer + sub-agent memory

- STATUS: CLOSED
- PRIORITY: 20
- TAGS: phase3,testing,quality,optional,deferred

> **OPTIONAL / DEFERRED.** The project has no test infrastructure
> today (no `tests/` dir, no pytest config, no CI). Bootstrapping
> that is real work and not on the critical path for the Phase 3
> rollout. Park this task; revisit once the rest of Phase 3 (3.3,
> 3.4, 3.5) is done. Priority dropped to 20 to reflect that.

## Why now

The smoke scripts run during 3.1 and 3.2 caught a real bug
(`defaultdict` phantom entries). Those scripts are gone after the
session ends. Phase 3.3 will add another non-trivial chunk of stateful
behaviour (per-agent message persistence, token-budget trim under
load). Before Phase 4 starts tuning budgets in production, we want a
safety net that:

- Re-runs the AC checks for every change.
- Catches regressions in the history layer specifically (the most
  load-bearing piece of the new architecture).
- Lets us refactor the trim policy with confidence in Phase 4.

Originally listed as Phase 4 in the master design doc ("Testing. Nice
to have, not a blocker"); promoted into Phase 3 so the new code lands
with coverage rather than retroactively.

## Scope

### Test infrastructure (one-time bootstrap)

1. **Add pytest dev dependency.** `pyproject.toml` — `pytest>=7` (and
   `pytest-asyncio` if any async tests land; defer until needed).
2. **Create `tests/` directory** with `tests/__init__.py` and
   `tests/conftest.py` (empty for now; placeholder for future fixtures).
3. **Add `[tool.pytest.ini_options]`** section to `pyproject.toml`:
   `testpaths = ["tests"]`, `addopts = "-q"`.
4. **README/dev note.** One-liner in the project entry-point docs (or
   a comment in `pyproject.toml`) on how to run: `pytest`. No new docs
   files.

### Tests to write

#### `tests/test_history.py` — covers Phase 3.1

Mirror the smoke-test assertions from the 3.1 implementation, plus
edge cases:

- **Backward compatibility**
  - `get_history(uid)` == `get_history(uid, "scufris")`
  - `get_history_with_new_message` returns the legacy dict shape
  - `clear_history` is a true alias for `clear_user`
- **Per-agent isolation**
  - Messages added under one agent don't appear under another
  - Two users with the same agent slot don't see each other's data
- **Token-budget trim**
  - Budget respected: total chars ≤ `budget * 4` after `add_messages`
  - Eviction is FIFO (oldest go first)
  - Never empties the slice (even if a single message exceeds the
    budget)
  - Message boundaries preserved (no splitting)
  - `BaseMessage` subtypes preserved (HumanMessage/AIMessage/
    ToolMessage round-trip identity)
- **Phantom-entry regression**
  - `get_message_count(uid)` and `get_history(uid, "foo")` after a
    `clear_user` do NOT inflate `get_stats()['total_users']`
- **Stats**
  - `messages_per_agent` correctly aggregates across users
  - Empty manager returns sensible defaults

#### `tests/test_sub_agent_memory.py` — covers Phase 3.2 + 3.3

Async or sync, depending on the chosen pytest-asyncio scope.

- **Plumbing (3.2)**
  - `AgentManager.process_message(messages, user_id)` puts
    `user_id` under `configurable` in the invoke config. Verified by
    a stub agent that records its invoke kwargs.
- **Sub-agent history (3.3)**
  - First call with empty slice: input messages = `[user_turn]`
    (no prior).
  - Second call with `keeps_history=True`: input messages =
    `[*persisted_prior, user_turn]`. The persisted prior matches the
    first call's user-turn + assistant reply.
  - `keeps_history=False` (utilities_agent): nothing is persisted
    across calls.
  - Missing `configurable.user_id` with `keeps_history=True`
    raises a clear `ValueError`.
  - Token budget honored across many calls (slice stays bounded).
- **Stub LLM strategy.** Don't hit Ollama. Either:
  - Inject a fake `ChatOllama` that returns canned messages, OR
  - Mock at the `agent.invoke` boundary so tests are deterministic
    and offline.
  Pick whichever requires less monkey-patching once 3.3 lands —
  current preference is mocking `agent.invoke` because the inner
  agent is opaque.

### Test style

- One assertion per concept; no mega-tests with 12 asserts.
- No network, no Ollama, no filesystem (history is in-process).
- Each test ≤ 20 lines of body.
- Test names read like specs:
  `test_clear_user_wipes_all_per_agent_slices_for_that_user`.

## Out of scope

- Tests for `cli.py` / `main.py` / Telegram handlers (covered
  end-to-end manually; these glue layers are mostly I/O).
- Integration tests against a real Ollama instance (Phase 4 eval
  harness territory).
- Tests for the Phase 1 prompt content or Phase 2 `context` arg
  composition (prompt-shape tests are brittle; covered in production
  smoke runs).
- CI wiring (no GitHub Actions today; out of scope for this task).

## Acceptance criteria

- [x] `pytest` runs from the project root with zero arguments and
      passes.
- [x] `tests/test_history.py` covers every public method of
      `ChatHistoryManager`, including the phantom-entry regression
      and the never-empty-slice invariant.
- [x] `tests/test_sub_agent_memory.py` covers Phase 3.2 plumbing
      and Phase 3.3 load+persist behaviour with stub agents (no
      Ollama calls).
- [x] Total test runtime ≤ 5 seconds (no I/O, no real LLMs).
- [x] No flaky tests (run 10 times in a row, all pass).

## Estimated effort

~2 hours. Mostly writing tests; the infra bootstrap is ~15 min.

## Dependencies

- **3.1** must be CLOSED before `test_history.py` can be written.
  ✅ Already CLOSED.
- **3.2** must be CLOSED before the plumbing tests in
  `test_sub_agent_memory.py` make sense. ✅ Already CLOSED.
- **3.3** must be CLOSED before the load+persist tests in
  `test_sub_agent_memory.py` can be written.

Recommended sequencing: ship 3.3 first, then this task, then the
remaining UI polish (3.4, 3.5) lands on top of a tested foundation.

## Post-hoc notes

- Bootstrap landed: `pytest>=9.0.3` added via `uv add --dev pytest` in
  the nix devshell (uv2nix rebuilds the venv on next `nix develop`).
  `[tool.pytest.ini_options]` block added to `pyproject.toml` with
  `testpaths = ["tests"]` and `addopts = "-q"`.
- 37 tests, 0.40s wall, 10/10 green on rerun.
- Stub strategy for sub-agent tests: monkey-patched both
  `agent_builder.ChatOllama` (sentinel object) and
  `agent_builder.create_agent` (returns a `_StubAgent` that records
  every `.invoke` and appends a canned `AIMessage`). This sidesteps
  the inner LangChain agent loop entirely. The stub agent itself
  uses sync `.invoke` rather than async; `AgentManager.process_message`
  is async and tested via `asyncio.run`. No `pytest-asyncio` needed.
- LangChain `@tool`-wrapped functions are invoked via
  `tool.invoke({"query": ..., "context": ...}, config={"configurable":
  {"user_id": N}})`. The `RunnableConfig` parameter on the wrapped
  function is auto-injected from this `config` kwarg.
- Six follow-up unit-test tatr tasks filed for the rest of the
  testable surface (telemetry, callbacks, stats, pure tools, HTTP
  tools, journal subprocess wrappers): `20260509-194005` through
  `20260509-194010`, priorities 18→13.
- One pre-existing `DeprecationWarning` surfaces under pytest:
  `utils/history.py:226` uses `datetime.utcnow()`. Not in scope here;
  worth a low-pri tatr if it's not already captured.

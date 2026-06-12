# Rewrite LLMCompactor without LangChain

- STATUS: CLOSED
- PRIORITY: 50
- TAGS: refactor,history,opencode

## Outcome

Closed `<2026-06-10>`.

LangChain is fully purged from the runtime. `rg -nP '^\s*(from|import)\s+langchain'`
returns zero hits across the repo (excluding `.venv/` and historical
`tasks/`). 324 pytest pass, ruff clean (lint + format), mypy clean,
end-to-end smoke against the live OpenCode daemon green.

### What shipped

1. **New `utils/messages.py`.** Tiny `HistoryMessage` frozen dataclass
   (`role: Literal["user","assistant","system","tool"]`, `content: str`)
   with `system_message`/`user_message`/`assistant_message` helpers.
   Replaces `langchain_core.messages.{Base,Human,AI,System,Tool}Message`
   wherever the project actually used them — i.e. as inert containers
   for `(role, content)` pairs.

2. **New `utils/tools/_decorator.py`.** Lightweight `Tool` class +
   `tool()` factory that preserves the `tool.invoke({...})` interface
   the existing pure / HTTP / journal tool tests rely on. Supports both
   bare `@tool` and `@tool("name")` forms (positional name override
   used by `weather_tool`). Avoids deleting the tool layer wholesale —
   a pragmatic shim that keeps the dead-but-tested surface alive
   without dragging in LangChain.

3. **Migrated `utils/tools/*`** (`calculator`, `datetime_tool`,
   `weather_tool`, `web_search`, `journal_tools`) from
   `from langchain.tools import tool` to `from ._decorator import tool`.
   No behaviour change; tests untouched.

4. **Rewrote `utils/memory_compactor.py`.** Dropped
   `langchain_core.messages.BaseMessage` and
   `langchain_ollama.ChatOllama`. New `OllamaChatTransport` does sync
   `httpx.Client` POSTs against Ollama's `/api/chat` with `stream=False`,
   exposing a single `chat(messages: list[{role, content}]) -> str`
   surface. `LLMCompactor` is now transport-agnostic — anything with a
   `.chat(...)` method plugs in. `_format_evicted` reads
   `HistoryMessage.role/.content` directly. `create_compactor` factory
   gained a `transport=` parameter alongside `model=` / `base_url=` so
   tests can inject fakes without monkeypatching internals. Lazy
   `import httpx` inside `chat()` keeps the noop path lightweight.

5. **Migrated `utils/history.py`.** `_histories` is now
   `Dict[..., List[HistoryMessage]]`. `add_user_message`/
   `add_ai_message`/`add_messages`/`_build_context_messages` rewritten
   against `HistoryMessage`. `get_history_with_new_message` no longer
   does `isinstance(msg, HumanMessage)` to recover the role — it just
   reads `msg.role` and `msg.content`. Public API unchanged.

6. **Stripped `utils/callbacks.py`.** Deleted the 580-line
   `ToolCallbackHandler` class + `_RunInfo` + `_peak_rss_kb` (dead
   code post-OpenCode swap; the runtime emits `ThinkingEvent` directly
   off the SSE stream now). Dropped `langchain_core.callbacks`,
   `langchain_core.messages`, `langchain_core.outputs`, `telegram`,
   `utils.telemetry`, `utils.logging`, `utils.telegram` imports. What
   stays: `DISPLAY_NAMES`, `SUB_AGENT_NAMES`, `display_name`,
   `is_sub_agent`, `_parse_tool_arg`, `_parse_tool_context`,
   `ThinkingEvent`, `ThinkingCallback`. File went 768 → 162 lines.
   `utils/__init__.py` updated to drop the `ToolCallbackHandler`
   re-export.

7. **Test surgery.**
   - `tests/test_history.py` — replaced
     `langchain_core.messages.{Human,AI,System}Message` with
     `HistoryMessage` constructors / role-based assertions
     (`type(m) is HumanMessage` → `m.role == "user"`).
   - `tests/test_memory_compactor.py` — same swap; the
     `RecordingCompactor` test double now stores `List[HistoryMessage]`.
   - `tests/test_memory_compactor_phase2.py` — replaced `_FakeLLM`
     (LangChain `AIMessage`-returning shape) with `_FakeTransport`
     (`.chat(messages) -> str`); preserved every existing assertion
     (markdown-fence stripping, malformed JSON degradation, factory
     env-opt-out, provenance, summary capping).
   - `tests/test_callbacks.py` — deleted ~155 lines of
     `ToolCallbackHandler` lifecycle + telemetry-handoff tests; kept
     ~120 lines of pure-helper tests (`display_name`, `is_sub_agent`,
     `_parse_tool_arg`, `_parse_tool_context`, `ThinkingEvent`).

8. **New `tests/test_compactor_http.py`** (5 tests). Exercises
   `OllamaChatTransport` against `httpx.MockTransport`: asserts the
   wire shape (POST `/api/chat`, `stream=False`, `messages` array,
   `options.temperature`), the response parsing (extracts
   `message.content`), graceful degradation when `message` is missing
   or `content` isn't a string, the full `LLMCompactor` round trip
   through the transport, and HTTP 503 → no-op fallback. Satisfies the
   "≥1 new test exercises compactor end-to-end against fake HTTP
   transport" acceptance criterion.

9. **`pyproject.toml` cleanup.** Dropped `langchain>=1.2.15`,
   `langchain-community>=0.4.1`, `langchain-ollama>=1.0.1`. Added
   explicit `httpx>=0.28.0` (used directly in `memory_compactor`,
   `opencode_client`, server routes — was a transitive dep). `uv lock`
   regenerated; the LangChain branch + transitives (`langgraph-*`,
   `langsmith`, `ollama`, `marshmallow`, `pydantic-settings`,
   `sqlalchemy`, `tenacity`, `xxhash`, `yarl`, `zstandard`, ...) are
   gone from `uv.lock`.

### Verification

```
$ rg -nP '^\s*(from|import)\s+langchain' --glob '!.venv'
tasks/20260509-150002/TASK.md:47:from langchain_core.runnables import RunnableConfig
tasks/20260509-150002/TASK.md:185:from langchain_core.callbacks import CallbackManagerForToolRun
```

Only old task documents (preserved as historical record) — runtime
+ tests are clean.

```
$ python -m ruff check .
All checks passed!
$ python -m ruff format --check .
57 files already formatted
$ python -m mypy .
Success: no issues found in 57 source files
$ python -m pytest
324 passed in 0.51s
$ python /tmp/.../smoke_http.py
/v1/healthz -> 200 {'status': 'ok'}
/v1/version -> 200 keys=[..., 'opencode_base_url', ...]
/v1/readyz -> 200 status=ready ollama=200 opencode=200
/v1/opencode/sessions -> 200 count=25
/v1/opencode/models -> 200 keys=['default', 'providers']
/v1/chat -> 200 body={'user_id': 99998, 'response': 'pong'}
/v1/clear -> 200 body={'user_id': 99998, 'cleared': 2, 'breakdown': {'scufris': 2}}
```

### Decisions

- **Option (a) over (b).** Direct Ollama `/api/chat` was the smaller
  delta (matched the existing sync `Compactor.compact()` contract).
  OpenCode's `POST /session/{id}/summarize` is a different shape
  entirely and would have meant rewriting the prompt template +
  losing model control. Re-evaluate if `[ollama]` becomes the only
  Ollama consumer.
- **Single `HistoryMessage` dataclass with a `role` field** rather
  than separate `UserMessage`/`AssistantMessage`/`SystemMessage`
  classes — fewer types to wire through, no `isinstance` ladder in
  `get_history_with_new_message`, and the test rewrites already had
  to touch every site anyway.
- **Deleted `ToolCallbackHandler` entirely** rather than porting it.
  After P100 the only remaining instantiations were in
  `tests/test_callbacks.py`; production now emits `ThinkingEvent`s
  directly via the OpenCode SSE listener. Keeping a 600-line
  LangChain-callback adapter alive for tests would have been pure
  waste.
- **Tools layer kept alive via `@tool` shim** rather than deleted.
  Production code post-P100 doesn't actually use any of these tools
  (the OpenCode runtime owns tool dispatch), but `tests/test_pure_tools.py`,
  `tests/test_http_tools.py`, `tests/test_journal_tools.py` are still
  meaningful integration tests for the underlying functions
  (calculator math, wttr.in parsing, journal pandas wiring). Cheaper
  to ship a 30-line shim than rewrite ~70 tests.
- **Added `httpx` as an explicit direct dependency.** It was
  transitively pulled in via LangChain / opencode-ai before; both
  paths still pull it now (opencode-ai stays, fastapi pulls
  starlette which pulls it for testclient), but we use it directly
  in `memory_compactor`, `opencode_client`, and several server
  routes — declaring it explicitly is the truthful manifest.

### Notes / follow-ups

- `opencode-ai>=0.1.0a36` is still listed in `pyproject.toml` even
  though `rg 'from opencode_ai'` returns zero hits. P100 bypassed
  the SDK in favour of raw httpx but didn't drop the dep. Leaving
  as a future cleanup — out of scope for the LangChain purge.
- The `LLMCompactor` is still wired only through `create_compactor`;
  `scufris_server/bootstrap.py` continues to force `NoopCompactor`
  pending a separate evaluation pass on whether compaction is worth
  the latency cost given the new OpenCode-driven prompt structure.

---

## Original task description (preserved)

# Rewrite LLMCompactor without LangChain

- STATUS: OPEN
- PRIORITY: 50
- TAGS: refactor,history,opencode

## Motivation

The OpenCode runtime swap (`tasks/20260610-101413`) deletes
`utils/agent_builder.py` and aims to drop the `langchain*` dependencies
from `pyproject.toml`. The remaining LangChain blocker is
`utils/memory_compactor.py::LLMCompactor`, which currently calls
Ollama through `langchain_ollama.ChatOllama` and constructs
`langchain_core.messages.{HumanMessage,SystemMessage,AIMessage}` to
drive a summary + facts extraction.

Until this is ported, the parent task either has to keep `langchain*`
in dependencies (defeating one of its acceptance criteria) or ship
with `NoopCompactor` wired in (degrading the long-term memory
behaviour). This task ports the compactor.

## Scope

### In

- Replace `LLMCompactor` with an implementation that does not import
  `langchain*`. Two reasonable options:
  - **(a)** Direct Ollama HTTP API (`POST /api/chat`), drop LangChain.
    Keeps the `[ollama]` config section. Smallest delta.
  - **(b)** Use OpenCode's `POST /session/{id}/summarize` endpoint
    (verified to exist in the SDK and the live server). Lets us drop
    Ollama entirely if no other consumer remains.
  - Recommend **(a)** — compaction is a low-stakes summarisation pass
    that doesn't need the full agent loop. Re-evaluate if `[ollama]`
    becomes the only Ollama consumer.
- Update `ChatHistoryManager` wiring so the compactor field type no
  longer references LangChain message classes.
- Update `utils/history.py` to construct prompts/messages as plain
  `dict` payloads (or a small dataclass) instead of
  `BaseMessage`/`HumanMessage`/etc.
- Update tests:
  - `tests/test_memory_compactor_phase2.py` and any other tests that
    monkeypatch `LLMCompactor` internals.
  - `tests/test_memory_tools.py` (if it pulls in the LangChain message
    classes via `history.py`).
- Remove `langchain`, `langchain-core`, `langchain-community`,
  `langchain-ollama` from `pyproject.toml` — verify no other importer
  remains.

### Out

- Reworking the compaction algorithm itself (window size, salvage
  thresholds, fact-extraction prompt). Pure transport swap.
- The `remember`/`forget` tools — they're handled by the parent task /
  the skills work, not here.

## Acceptance criteria

- [x] `utils/memory_compactor.py` does not import any `langchain*`
      module.
- [x] `utils/history.py` does not import any `langchain*` module.
- [x] `pyproject.toml` no longer lists `langchain`, `langchain-core`,
      `langchain-community`, or `langchain-ollama`.
- [x] `rg -nP "^\s*(from|import)\s+langchain"` returns zero hits in
      the repo (excluding `.venv/`).
- [x] Compaction events (`kind="compaction"`) still surface in the SSE
      thinking stream with the same shape consumed by `cli.py`.
- [x] `nix flake check` passes (ruff, mypy, pytest).
- [x] At least one new test exercises the compactor end-to-end against
      a fake HTTP transport (no LangChain stubbing).

## Open questions

- Keep Ollama as the compactor model, or switch to OpenCode? Decision
  point above.
- Compaction can be slow (full LLM call). It already happens
  off-the-hot-path in `ChatHistoryManager.maybe_compact`; verify that
  contract still holds with the new transport (no event-loop blocking
  if we're running inside FastAPI).
- The summarisation prompt currently relies on system + human + ai
  message ordering. If we go with Ollama's `POST /api/chat`, the
  payload format is similar (`messages: [{role, content}, …]`). If we
  go with OpenCode, the system prompt + user prompt go via the
  `/session/{id}/summarize` body — different shape entirely.

## References

- `tasks/20260610-101413/TASK.md` — parent task; "Out" section
  explicitly defers this work.
- `tasks/20260610-101413/SCHEMA.md` — confirms
  `POST /session/{id}/summarize` exists.
- `utils/memory_compactor.py` — current implementation.
- `utils/history.py` — calls into the compactor; uses LangChain
  message classes for the window.
- `tests/test_memory_compactor_phase2.py` — existing tests to migrate.

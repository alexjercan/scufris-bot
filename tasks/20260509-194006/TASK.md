# Unit tests — utils/callbacks.py (parsers + dispatch)

- STATUS: OPEN
- PRIORITY: 17
- TAGS: testing,quality

Cover the pure helpers in `utils/callbacks.py` plus enough of
`ToolCallbackHandler`'s lifecycle to lock in the telemetry hand-off
behaviour.

## Scope

`tests/test_callbacks.py`:

### Pure helpers

- `display_name`
  - Known names map via `DISPLAY_NAMES`.
  - Unknown names fall back to Title Case with underscores → spaces.

- `is_sub_agent`
  - True for any name in `SUB_AGENT_NAMES`.
  - True for arbitrary `"foo_agent"`.
  - False for `"weather"`, `"calculator_tool"`, etc.

- `_parse_tool_arg`
  - JSON dict with `query` → returns the query string.
  - JSON dict with `__arg1` → returns it.
  - JSON dict with multiple keys, no preferred → returns first scalar.
  - Python repr (`"{'__arg1': 'Ploiesti'}"`) → still parsed via
    `ast.literal_eval`.
  - Bare string → returns as-is.
  - Empty string → `None`.

- `_parse_tool_context`
  - Dict with non-empty `context` → returns it.
  - Dict with empty `context` → `None` (cold-start delegations stay
    quiet).
  - Dict without `context` → `None`.
  - Non-dict / unparseable → `None`.

### ToolCallbackHandler lifecycle

Use a `ThinkingCallback` that appends events to a list, and run a
synthetic `on_tool_start` → `on_tool_end` pair:

- Sub-agent call (e.g. `name="knowledge_agent"`, input
  `{"query": "weather", "context": "RO"}`) emits one `tool_call`
  event with `arg="weather"` and `context="RO"`.
- After `on_tool_end` with `output.content="cannot_handle: foo"`,
  if telemetry is enabled (monkeypatch + tmp log file) the JSONL
  record carries `outcome="refused"`.
- Same with normal output → `outcome="ok"`.
- `on_tool_error` → `outcome="error"`.

Fakes: a tiny `_FakeOutput` class with `.content`, plus stub UUIDs
from `uuid.uuid4()`. No real LLM, no network.

## Out of scope

- LLM callbacks (`on_llm_start` / `on_llm_end` / reasoning extraction).
  Cover those only if a regression bites — extraction logic is
  format-fragile and will rot quickly under test.
- Chain callbacks (filtered to anonymous wrappers; low value).
- `_peak_rss_kb` (platform-dependent).

## Acceptance criteria

- [ ] Pure-helper tests are deterministic and instant.
- [ ] Lifecycle tests don't touch real LLM or network.
- [ ] Telemetry-handoff assertions use a tmp log file + env
      monkeypatching; nothing leaks into `logs/`.

## Dependencies

- Test bootstrap from Phase 3.6 (`tasks/20260509-171311`).
- Telemetry tests task (`tasks/20260509-194005`) — share patterns
  for stubbing the log file.


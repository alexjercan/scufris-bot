# Unit tests — HTTP tools (weather, web_search, opencode) with mocks

- STATUS: CLOSED
- PRIORITY: 14
- TAGS: testing,quality

Cover the three network-bound tools with HTTP / SDK mocks so the
behaviour around clamps, parsing, and error handling is locked in
without hitting real services.

## Scope

`tests/test_http_tools.py`:

### weather_tool

Use `monkeypatch` on `utils.tools.weather_tool.requests.get` to return
a fake `Response` with a fixture `j1` payload (subset is fine — must
include `current_condition`, `nearest_area`, and a 3-element `weather`
array each with an 8-slot `hourly`).

- `forecast_days=0` → response contains "Current weather in …" and
  does NOT contain "Forecast:".
- `forecast_days=2` → response contains "Forecast:" and exactly 2
  date lines.
- Defensive clamp: `forecast_days=99` → rendered with 3 days (the
  full fixture array).
- `forecast_days=-1` → no forecast block.
- Invokes `requests.get` with `format=j1` URL and `timeout=10`
  (assert call args).
- `requests.exceptions.Timeout` → "Weather request timed out".
- `requests.exceptions.RequestException` → "Failed to fetch weather".
- Bogus payload (`{}`) → "Failed to parse weather data".

### web_search_tool

Mock `utils.tools.web_search.DDGS` with a stub class whose
`__enter__` returns an object with a `text(...)` method:

- Returns N results → output contains numbered items (`1.`, `2.`)
  and a `"📚 References:"` block with one `[N] url` line per result.
- Returns `[]` → `"No results found for the query."`.
- `text()` raises → output starts with `"Search failed:"`.

### opencode_tool

Mock `utils.tools.opencode_tool.Opencode` (the SDK client class):

- Happy path: `session.create()` returns object with `.id`,
  `session.chat()` returns object with `.parts=[obj_with_text]`,
  output equals the joined parts text. `session.delete()` called
  with the id.
- Empty parts → returns `"OpenCode completed but returned no output."`.
- `APIConnectionError` raised → output contains
  `"Cannot connect to OpenCode server"` and the `opencode serve`
  hint.
- Generic exception with "authentication" in the message → returns
  the auth-help block.
- Generic exception otherwise → returns the catch-all error block.

## Out of scope

- Real network calls.
- Response shape changes from upstream APIs (we test against fixtures,
  not contracts).

## Acceptance criteria

- [x] No test makes a real HTTP request.
- [x] Each tool has at least one happy-path + one failure-path test.

## Post-hoc notes

- Landed as `tests/test_http_tools.py` (16 tests, ~0.4s).
- **Gotcha**: `utils/tools/__init__.py` does
  `from .weather_tool import weather_tool`, which rebinds the parent
  package's `weather_tool` *attribute* to the StructuredTool. So
  `import utils.tools.weather_tool as weather_mod` yields the tool
  object, not the module. Use
  `weather_mod = sys.modules["utils.tools.weather_tool"]` to grab
  the real module for monkeypatching `requests.get`. Same trick for
  `web_search` and `opencode_tool`.
- Bogus `{}` payload doesn't trigger the parsing-error branch (because
  `.get` defaults handle every key); test asserts forecast is omitted
  and location is rendered verbatim instead.
- For `APIConnectionError`, the SDK constructor requires a `request`
  arg; passing `request=None` works in tests because the handler only
  reads the message.

## Dependencies

- Test bootstrap from Phase 3.6 (`tasks/20260509-171311`).
- `weather_tool` clamp behaviour is documented in
  `tasks/20260509-165118` (the legacy-tool sweep).


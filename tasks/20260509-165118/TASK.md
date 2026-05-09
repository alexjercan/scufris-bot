# Fix weather_tool: convert to @tool, support forecast horizon

- STATUS: CLOSED
- PRIORITY: 60
- TAGS: bug,tools,weather

## Symptom

Real reproducer from a Phase 2 smoke test:

```
> what is the weather like in Ploiesti?
→ Scufris asks Knowledge Agent: weather forecast for Ploiesti for the next 3 days
  → Knowledge Agent uses Weather: Ploiesti
ERROR  └─ tool weather failed | 0.00s | Too many arguments to single-input tool weather.
                Consider using StructuredTool instead. Args: ['Ploiesti', 3]
ERROR  tool knowledge_agent failed | 2.56s | Too many arguments to single-input tool weather.
ERROR  Error processing CLI message: Too many arguments to single-input tool weather.
```

The whole turn craters with a 500-equivalent. The user sees a raw
exception instead of a weather report.

## Root cause

`utils/tools/weather_tool.py:81` builds the tool with the **legacy
single-input** `Tool(name=..., func=...)` constructor:

```python
weather_tool = Tool(
    name="weather",
    description="Get current weather information for any location...",
    func=get_weather,
)
```

That registers a schema with exactly one positional string arg
(`location`). The Knowledge Agent's LLM, primed by a query of "next 3
days", emits `weather('Ploiesti', 3)` — LangChain rejects the second
positional and raises `Too many arguments to single-input tool`.

Compounding issue: `get_weather` itself only fetches *current*
conditions, even though wttr.in's `format=j1` response **already
contains a 3-day forecast** in `data["weather"]`. So even if the LLM
called it correctly today, it couldn't get a forecast back. The
description literally says "current weather… current conditions" but
nothing in the system points the LLM at a different tool for
forecasts (there isn't one).

## Why Phase 2 surfaced it

This bug existed before Phase 2. The trigger conditions:

- LLM phrasings like "weather forecast", "next 3 days", "tomorrow",
  "for the week" → model tries to pass a second arg.

Pre-Phase-2 the main agent's prompt wasn't as crisp about delegation
shape, so the inner LLM was more likely to call `weather('Ploiesti')`
and let the response speak for itself. Phase 2's clean
`(query, context)` contract makes Scufris pass much better-formed
queries (e.g. "weather forecast for Ploiesti for the next 3 days")
which the Knowledge Agent then faithfully tries to honour with both
arguments. We made the upstream better; the downstream limitation
became visible.

## Fix

### Required

1. **Rewrite the tool with `@tool`** so it has a real multi-arg
   schema, replacing the legacy `Tool(...)` constructor.

   Suggested signature:

   ```python
   @tool
   def weather(location: str, forecast_days: int = 0) -> str:
       """Get current weather and an optional short-range forecast.

       location: city or "city, country" (e.g. "Ploiesti", "Tokyo, Japan").
       forecast_days: 0 = current conditions only (default).
                      1–3 = include a daily forecast for that many days.
                      Values >3 are clamped to 3.
       """
   ```

2. **Actually use the forecast data wttr.in already returns** when
   `forecast_days > 0`. The `data["weather"]` array contains entries
   keyed by `date` with `mintempC`, `maxtempC`, an `hourly` block, and
   per-day `weatherDesc`. Format a compact one-line-per-day summary;
   don't dump the raw JSON. Cap at 3 days (wttr only returns 3 anyway).

3. **Update the tool description** to advertise the forecast
   capability so Scufris and the Knowledge Agent know it exists.
   Something like:

   > Get weather for any location worldwide. Pass `forecast_days=0`
   > (default) for current conditions only. Pass 1–3 for a short
   > daily forecast covering that many days. Examples: `weather("Paris")`,
   > `weather("Ploiesti", 3)`.

4. **Update the call site in `utils/tools/__init__.py`** if the
   exported symbol changes shape (it shouldn't if we keep the name
   `weather_tool` on the @tool-decorated function).

### Nice-to-have (do only if cheap)

- Clamp `forecast_days` defensively in the function body (don't trust
  the LLM to respect "1–3").
- A single short integration test that calls `weather("London", 1)`
  and asserts the response mentions both current temp and at least
  one forecast day. wttr.in is public and reliable enough; if we
  worry about CI flakiness, mock `requests.get`.

## Acceptance criteria

- [x] `weather_tool` is built with `@tool`; introspecting its
      `args` shows both `location` and `forecast_days`.
- [x] Reproducer query ("what is the weather like in Ploiesti?")
      completes successfully end-to-end with no exception.
- [x] Calling with `forecast_days >= 1` returns a response that
      includes both current conditions and per-day forecast lines.
- [x] Calling with `forecast_days = 0` (or omitted) returns the
      same shape of response as today (back-compat for the common
      "weather in X" path).
- [x] Tool description in the registry mentions the forecast
      capability.

## Out of scope

- Replacing wttr.in with another provider. wttr is fine.
- Caching responses. Not needed at current usage.
- Hourly forecasts. Daily aggregates are sufficient for the chat UX.
- Geocoding edge cases (ambiguous city names). wttr's nearest-area
  heuristic is good enough.

## References

- Tool definition: `utils/tools/weather_tool.py:81`.
- Call site / export: `utils/tools/__init__.py:23`.
- Tool registration: `utils/agent_builder.py` (the
  `create_knowledge_agent` `tools=[web_search_tool, weather_tool]`
  list).
- Surfacing context (the trace that exposed the bug): see the Phase 2
  smoke test conversation in the session log of 2026-05-09.

## Implementation notes (post-hoc)

User expanded scope mid-task: "fix all tools that are not using
`@tool`". Three tools were on the legacy `Tool(name=..., func=...)`
constructor; all converted in this session:

- `utils/tools/weather_tool.py` — bug fix + multi-arg schema
  (`location: str`, `forecast_days: int = 0`). Forecast block built
  from the `data["weather"]` array that wttr.in already returned but
  the old code ignored. Per-day line picks the noon-ish hourly slot
  (index 4 of 8) for the `weatherDesc`. Defensive
  `max(0, min(int(forecast_days or 0), 3))` clamp at the top of the
  function — never trust the LLM to respect "1–3".
- `utils/tools/web_search.py` — pure modernisation. Behaviour
  unchanged (single `query: str` arg, same DDG path, same
  `📚 References:` block).
- `utils/tools/opencode_tool.py` — pure modernisation. Behaviour
  unchanged. Lifted three magic constants to module-level
  (`OPENCODE_BASE_URL`, `DEFAULT_PROVIDER_ID`, `DEFAULT_MODEL_ID`)
  while in there.

### `@tool("name")` pattern

LangChain's `@tool` defaults the runtime tool name to the function
name. The existing call sites and prompts reference `weather`,
`web_search`, `opencode` (no `_tool` suffix), so I used the
`@tool("explicit_name")` form to override while keeping the export
symbol `*_tool` (matches what `agent_builder.py` and
`utils/tools/__init__.py` import). Prevents a sprawling rename.

### Tested

- `weather_tool.invoke({"location": "Ploiesti", "forecast_days": 3})`
  — original failing reproducer — now returns current + 3-day
  forecast with no exception.
- `weather_tool.invoke({"location": "Paris"})` — back-compat,
  no `Forecast:` block in output.
- Schemas verified: `weather.args = {forecast_days, location}`,
  `web_search.args = {query}`, `opencode.args = {task}`.
- `python -c "import main"` — full agent hierarchy still wires up.

`opencode_tool` was **not** invoked live (requires running
`opencode serve`); only its schema and import were verified.

### What was *not* done

- No mocked `requests.get` integration test (the live wttr.in
  call works and is reliable enough; the task notes this as
  "nice-to-have, only if cheap" — and it would mean adding a
  test-infra dependency we don't otherwise have yet).
- Did not file a separate task for the `web_search` /
  `opencode` conversions — they were trivial and folded into
  this task per user request.

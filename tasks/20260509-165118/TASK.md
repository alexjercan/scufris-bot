# Fix weather_tool: convert to @tool, support forecast horizon

- STATUS: OPEN
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

- [ ] `weather_tool` is built with `@tool`; introspecting its
      `args` shows both `location` and `forecast_days`.
- [ ] Reproducer query ("what is the weather like in Ploiesti?")
      completes successfully end-to-end with no exception.
- [ ] Calling with `forecast_days >= 1` returns a response that
      includes both current conditions and per-day forecast lines.
- [ ] Calling with `forecast_days = 0` (or omitted) returns the
      same shape of response as today (back-compat for the common
      "weather in X" path).
- [ ] Tool description in the registry mentions the forecast
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

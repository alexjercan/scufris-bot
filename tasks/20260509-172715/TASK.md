# Phase 3.4b — Richer `/stats` + deprecate `/history`

- STATUS: CLOSED
- PRIORITY: 65
- TAGS: phase3,cli,telegram,observability

> Builds on **Phase 3.4** (`tasks/20260509-170254`), which lands the
> first `/stats` command with per-agent message counts. This task
> grows `/stats` into a real session dashboard and starts the
> deprecation cycle for the old `/history` command. Distinct from the
> heavier **telemetry spike** (`tasks/20260509-165516`), which logs
> per-invocation JSONL traces for offline analysis — `/stats` is
> live, in-session, human-readable.

## Scope

### A. Extend `/stats` output (CLI + Telegram)

Target rendering (CLI):

```
Scufris session stats
─────────────────────
Uptime:       1h 23m
Model:        qwen3:latest @ http://localhost:11434
Sub-agent invocations: 17

Per-agent memory:
  scufris          8 msgs   1.2k chars  (~300 tok)   [main flow]
  knowledge_agent  6 msgs   3.1k chars  (~775 tok / 4000 budget, 19%)   calls=4   last=2m ago
  coding_agent     0 msgs                            calls=0
  journal_agent    4 msgs   2.0k chars  (~500 tok / 8000 budget, 6%)    calls=3   last=12m ago
  utilities_agent  —                                                    calls=10  (history disabled)

Totals: 18 messages across 4 agents
```

Telegram render is the same content in monospace ``` blocks (no
fancy box drawing — Telegram's monospace handles alignment).

### B. New data points to surface

1. **Char-proxy tokens per slice.** Already cheap — sum of
   `len(str(m.content))` divided by `_CHARS_PER_TOKEN` (4). Add a
   helper on `ChatHistoryManager`:
   ```python
   def get_token_estimate(self, user_id: int, agent: str) -> int:
       msgs = self._histories.get((user_id, agent), [])
       return sum(len(str(m.content)) for m in msgs) // _CHARS_PER_TOKEN
   ```

2. **Per-agent budget + utilization.** Budget is set at sub-agent
   build time (`history_token_budget` kwarg). It currently lives in
   the closure inside `sub_agent_tool` and is not introspectable.
   Two options:
   - **(B-i) Track in `ChatHistoryManager`.** Add
     `register_agent(agent, token_budget, history_disabled=False)`
     called from `create_sub_agent` at build time. `get_stats()`
     picks budgets up from this registry.
   - **(B-ii) Pass a budget map into `setup_scufris`.** Less
     coupling but duplicates the constants. Recommend B-i.

3. **Session uptime + model.** `setup_scufris` (or the CLI/main
   entrypoint) records `started_at = datetime.utcnow()` and the
   `Config.ollama_model` / `Config.ollama_base_url` strings. Pass
   this snapshot into the `/stats` handler. For Telegram, the
   handler lives in a long-running process so this is straightforward.

4. **Per-agent invocation count.** Requires a counter incremented
   inside `sub_agent_tool` on every call. Add to the
   `ChatHistoryManager` registry from B-i:
   ```python
   def record_invocation(self, user_id: int, agent: str) -> None:
       self._invocations[(user_id, agent)] += 1
   ```
   Increment from inside `sub_agent_tool` (top of the call, after
   the `user_id` resolve, regardless of `keeps_history`).

5. **Last-activity timestamp per agent.** Same registry, set in the
   same place as B-4:
   ```python
   self._last_activity[(user_id, agent)] = datetime.utcnow()
   ```
   Render as a relative duration ("2m ago", "12m ago", "—" if never).
   Helper `format_relative(ts) -> str` in a small CLI utility.

### C. Deprecate `/history`

`/history` keeps working but prints a one-line deprecation notice
*before* its existing output:

```
[deprecated] /history will be removed; use /stats instead
messages in this session: 8
max per user: 20
total users: 1
total messages: 18
```

File a follow-up task `Phase 3.4c — remove /history` (priority 30,
**deferred**). Don't remove yet — keep one transition window.

### D. Files to touch

- `utils/history.py` — new helpers: `get_token_estimate`,
  `register_agent`, `record_invocation`, `_last_activity`,
  `_invocations`, `_budgets`, `_history_disabled`. Extend
  `get_stats()` to include all of the above.
- `utils/agent_builder.py` — call `history_manager.register_agent(...)`
  inside `create_sub_agent` build time; call
  `history_manager.record_invocation(user_id, name)` inside
  `sub_agent_tool` body (top, after user_id resolve).
- `cli.py` — replace `/stats` rendering; add deprecation notice to
  `/history`; capture session start time + config snapshot at
  startup and thread into the slash-command handler.
- `main.py` — same treatment for Telegram `stats` and `clear_history`
  handlers (clear can also use the richer breakdown text).
- `tatr` — file `Phase 3.4c — remove /history` (priority 30,
  STATUS: DEFERRED).

## Open design questions

- **Q1.** Are sub-agent invocations counted per-user or globally?
  Default proposal: **per-user** (matches the per-user history
  scoping). Globally would require a separate counter dict keyed
  only by `agent`. Per-user lets us scope `/stats` to the requesting
  user's activity.
- **Q2.** When `utilities_agent` has `keeps_history=False`, do we
  still show invocation count + last-activity? **Yes** — those are
  about call traffic, not about memory.
- **Q3.** Counters reset on `/clear`? Proposal: **no**. `/clear`
  wipes memory, not telemetry. Add a separate `/reset-stats` if we
  ever want it (don't file the task now).

## Out of scope

- JSONL telemetry log (covered by `tasks/20260509-165516`).
- Cost/latency tracking (would require timing wrappers + token
  estimates from Ollama responses; file separately if desired).
- Per-tool call counters (different layer entirely).
- Removing `/history` entirely (filed as 3.4c, deferred).

## Acceptance criteria

- [x] `/stats` output includes uptime, model+base URL, total
      sub-agent invocations, and the per-agent table with msgs /
      chars / token-estimate / budget / utilization% / call count /
      last-activity columns. *(Verified via `format_stats_lines`
      smoke test — sample output recorded below.)*
- [x] `register_agent` is called for all four sub-agents at build
      time; `keeps_history=False` agents are flagged in the registry
      and rendered without budget/utilization columns. *(Confirmed:
      utilities_agent renders `(history disabled)` line; the other
      three render with budget %.)*
- [x] Invocation count increments exactly once per `sub_agent_tool`
      call, including for `cannot_handle` returns. *(Counter is
      bumped at the very top of the tool body, before any branching.)*
- [x] Last-activity timestamp updates on every invocation;
      `format_relative` returns "—" for agents never called.
      *(Verified: 12-min-old timestamp renders "12m ago"; cold
      agents render "—".)*
- [x] `/history` still prints its old output but prefixed with the
      deprecation notice. Manual test: run `/history`, confirm
      notice + old format both appear. *(CLI prints
      `[deprecated] /history will be removed; use /stats instead`;
      Telegram prepends `⚠️ /history is deprecated; use /stats
      instead.`)*
- [x] Telegram `/stats` and `/clear` mirror the CLI content (modulo
      monospace formatting). *(Telegram wraps `format_stats_lines`
      output in a Markdown ``` block for column alignment.)*
- [x] Counters survive `/clear` (Q3 above). *(`clear_user` only
      touches `_histories`; `_invocations` and `_last_activity` are
      untouched.)*
- [x] Follow-up task `Phase 3.4c — remove /history` filed with
      STATUS: DEFERRED, priority 30. *(Filed as
      `tasks/20260509-174354`.)*

## Sample `/stats` output

```
Scufris session stats
─────────────────────
Uptime:                1h 23m
Model:                 qwen3:latest @ http://localhost:11434
Sub-agent invocations: 3

Per-agent memory:
  coding_agent       0 msgs                              calls=0   last=—
  journal_agent      0 msgs                              calls=0   last=—
  knowledge_agent    2 msgs   ~150 tok / 4000 budget, 3%        calls=2   last=0s ago
  utilities_agent    (history disabled)   calls=1   last=12m ago

Totals: 2 messages across 1 agent(s)
```

## Implementation notes

- New module `utils/stats.py` owns rendering (`format_relative`,
  `format_uptime`, `format_stats_lines`) so CLI + Telegram share one
  source of truth.
- `ChatHistoryManager` gained: `_agent_registry`, `_invocations`,
  `_last_activity` dicts; `register_agent`, `record_invocation`,
  `get_token_estimate`, `get_user_telemetry` methods. `get_stats`
  also now returns `total_invocations`.
- `create_sub_agent` resolves `user_id` once at the top of
  `sub_agent_tool` (not just inside the `keeps_history` branch) so
  telemetry works for stateless agents too.
- `create_utilities_agent` now forwards `history_manager` into
  `create_sub_agent` (was previously `_ = history_manager`) so the
  registry sees it.
- Session start time captured in CLI `settings` dict and as
  module-level `session_started_at` in `main.py`.
- Open question Q3 resolved: counters survive `/clear` (matches the
  proposal — `/clear` is about memory, not telemetry).

## Known follow-ups

- `datetime.utcnow()` is deprecated in Python 3.12+. Switch to
  `datetime.now(UTC)` codebase-wide as a separate cleanup pass — not
  filed as a tatr task yet, low priority.

## Dependencies

- **Hard-blocks-on Phase 3.4** (`tasks/20260509-170254`) — that task
  scaffolds the `/stats` command shell and the Telegram counterpart.
  This task only extends the rendering and adds the registry layer.
- **Soft-blocks-on Phase 3.3** (CLOSED) — needs `sub_agent_tool` to
  be the central invocation site for the counter increment. Already
  true.

## Estimated effort

~1.5–2 hours. Most of the work is in the registry plumbing
(`ChatHistoryManager` gains four new dicts and three new methods)
and the render formatting. Telegram parity is mechanical once CLI
is right.

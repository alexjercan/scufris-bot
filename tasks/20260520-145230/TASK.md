# Tool usage histogram in /stats

- STATUS: CLOSED
- PRIORITY: 50
- TAGS: observability,cli,backlog

## New Data Collected

- `ChatHistoryManager` (or a thin wrapper around the callback handler) tracks per-`(user_id, tool_name)` call counts, already partially done via `_invocations`
- Extend to track **all** tools, not just sub-agents — `web_search`, `weather`, `calculator`, `macros_entry`, etc.

## /stats Output Addition

```
Tool usage (this session):
  web_search        ████████  8
  weather           ███       3
  macros_entry      ██        2
  tasks_entry       █         1
  calculator        █         1
```

ASCII bar chart normalized to the max call count — fits in a terminal, works in Telegram monospace block.

## Implementation

- `ToolCallbackHandler.on_tool_end` already fires for every tool — increment a `Counter` keyed by tool name there
- `get_user_telemetry` extended to include the tool counter
- `format_stats_lines` gets a second section below the per-agent table

## Dependencies on User Identity (`20260520-145231`)

**Moderate** dependency. Counters are already `user_id`-keyed today, but with the unified identity layer the meaning sharpens:

- Counts merge correctly across surfaces — "8 web_search calls today" includes calls made from both CLI and Telegram by the same user
- New surface breakdown becomes possible: `/stats --by-surface` could show `web_search: 6 cli, 2 telegram`
- Lifetime counters (not just session) become meaningful once `messages` is persisted (`20260520-145229`) and identity is stable — without identity, "lifetime" is misleading because the same human has two phantom user IDs

## Persistence Tie-in (`20260520-145229`)

For lifetime stats, derive counts on demand from the persisted `messages.tool_calls_json` rather than maintaining a separate counter table:

```sql
SELECT json_extract(tc.value, '$.name') AS tool, COUNT(*)
FROM messages, json_each(messages.tool_calls_json) tc
WHERE user_id = ? AND ts > ?
GROUP BY tool
ORDER BY 2 DESC;
```

In-memory `Counter` stays for current-session stats (fast); SQLite query backs the "all time" view.

## Complexity Estimate

Small. ~1 day of work: increment in the callback, render the histogram, surface in `/stats`. The all-time variant adds another day once persistence lands.

## Open Questions

- **Time window**: session-only, today-only, 7-day, all-time, or all four toggleable? My instinct: session by default, `/stats --since 7d` to widen.
- **Latency / failure tracking**: also show average duration per tool and failure count? Very useful for spotting broken integrations, but bigger scope.
- **Sub-agent attribution**: when the journal sub-agent calls `macros_entry`, is that counted under journal or under the tool? Probably both — group by `(agent, tool)`.
- **Telegram rendering**: monospace block is fine for ASCII bars, but Telegram has 4096-char message limits — cap to top 10 tools by default.

## Resolution

Landed in one pass alongside the broader Telegram-`/stats` prettify and
collapsible thinking-trace work the user asked for at the same time.

- `ChatHistoryManager` grew `_tool_invocations: Dict[(user_id, tool_name), int]`,
  with `record_tool_invocation` + `get_tool_invocations`, mirroring the
  `_invocations` counter (survives `/clear`).
- `ToolCallbackHandler.__init__` accepts optional `history_manager` +
  `user_id`; `on_tool_end` records every tool call (sub-agents too —
  the histogram is the cross-cutting view, the per-agent table keeps
  its own column tracked via `record_invocation` from `sub_agent_tool`).
- `scufris_server/routes/chat.py:_run_turn` now always injects a
  per-user counter handler so both `/chat` and `/chat/stream` log
  histogram traffic. The streaming-only handler no longer carries
  `history_manager`/`user_id` to avoid double-counting.
- `format_stats_lines` appends a `Tool usage:` section with an ASCII
  histogram (top 10, normalised to max). Capped via `hasattr` so
  in-tree fakes that don't implement `get_tool_invocations` keep
  working.
- `StatsResponse` schema gained `tools: Dict[str, int]` and a
  `summary: StatsSummary` block (uptime, model, base_url, totals) so
  Telegram can render without reparsing `lines`.
- New `format_telegram_stats(payload)` in `bot.py` produces a clean
  legacy-Markdown layout: bold headings, inline-code values (with
  underscore escaping), per-agent + histogram in fenced code blocks
  for monospace alignment. `/stats` Telegram handler swapped to it.
- Final-answer messages now post with an inline-keyboard toggle
  (`💭 Show thinking ▼` / `💭 Hide thinking ▲`). Per-message cache
  `_thinking_cache` (FIFO, capped at 256) holds `(answer, trace)`;
  `CallbackQueryHandler(thinking_toggle, pattern=r"^think:")` edits
  between collapsed and expanded views, falling back to a plain edit
  on Markdown parse failures and a "(thinking trace expired)" notice
  on stale post-restart taps.
- Per-event renderer prettified: per-tool emoji (🔍 web_search,
  🌤 weather, 🧮 calculator, 🤖 sub-agent, 🕒 datetime, 💻 opencode,
  🔧 fallback), tree branch (`└─`) for nested calls, 💭 marker on
  reasoning text.
- 21 new tests added (370 total, up from 349); all 18 nix flake
  checks (ruff, mypy, pytest, scufris-vm, both modules) pass green.

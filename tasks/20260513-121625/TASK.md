# Refactor utilities_agent into os_agent with file_management + system_monitor sub-agents

- STATUS: OPEN
- PRIORITY: 65
- TAGS: refactor,agents

## Goal

The current `utilities_agent` is a grab-bag of pure functions
(calculator, datetime). Repurpose the slot as a proper "OS agent"
that delegates to two narrow sub-agents — `file_management_agent` and
`system_monitor_agent` — covering local filesystem and host-state
queries respectively. Pure-function tools move under whichever child
makes sense, or stay on the OS agent itself.

## Scope

### In
- Rename `create_utilities_agent` → `create_os_agent` in
  `utils/agent_builder.py`. Keep `create_utilities_agent` as a
  deprecated alias for one cycle.
- Add `create_file_management_agent`:
  - tools: `find_file`, `read_file`, `list_dir`, `grep_file`,
    `file_stat`. All read-only for v1.
  - keeps_history: True (small budget; useful for "the file from
    earlier").
- Add `create_system_monitor_agent`:
  - tools: `disk_usage`, `memory_info`, `cpu_load`, `battery_status`,
    `process_list` (top N by RSS), `uptime`.
  - keeps_history: False (snapshots are stateless).
- `os_agent` keeps `calculator_tool` + `datetime_tool` directly and
  delegates filesystem / monitoring questions down.
- Prompt updates: rewrite `OS_AGENT_PROMPT` to make the delegation
  contract clear (when to call which child).
- Tests: per-tool unit tests for the new tools, agent-builder smoke
  tests confirming the hierarchy registers cleanly.

### Out
- Write/mutate filesystem tools (rm/mv/chmod) — separate task once
  we trust the read path.
- Anything Windows-specific. Linux + macOS only for v1.
- Network monitoring (covered by future agents).

## Acceptance criteria

- `os_agent` shows up in `/stats` with two registered children
  matching the names above.
- Asking the CLI "how much free disk on /" or "show me the largest
  file under ~/Downloads" routes through the right sub-agent and
  returns sensible answers.
- All existing tests still pass; new tools have ≥80% line coverage
  in `tests/`.
- README's agent-overview gets a one-line update.

## Notes

- Use `psutil` for system stats — already a transitive dep, exposes
  cross-platform numbers cleanly.
- Read-only file tools should refuse paths outside `~`/`/tmp` by
  default, with an env-var override for power users.

## References

- `utils/agent_builder.py:638` — current `create_utilities_agent`.
- `tasks/20260513-121623/TASK.md` — agent v2 spike.

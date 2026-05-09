# Improve logging in the project

- STATUS: CLOSED
- PRIORITY: 70
- TAGS: chore

## Goal

Surface much richer diagnostics about what the agent hierarchy is doing —
which sub-agent was called, which tools each one used, how long each call
took, how big the inputs/outputs were, and (optionally) memory pressure.
This is mostly for use from the CLI, where DEBUG is now the default.

## Plan

### 1. Rework `ToolCallbackHandler` (`utils/callbacks.py`)

- Decouple from Telegram: `telegram_transport` becomes `Optional` so the
  CLI can use the same handler with no transport.
- Track each run by its `run_id` instead of using single instance vars
  (the current code breaks on nested/concurrent calls — sub-agent calls a
  tool, both share `_tool_start_time`).
- Compute a **depth** for every run via `parent_run_id`, so log lines can
  be indented to mirror the call tree:
  ```
  [tool] knowledge_agent  in=42c
    [llm]  ChatOllama start
    [llm]  ChatOllama done in 1.23s | 412 tokens
    [tool]   web_search   in=18c
    [tool]   web_search   done in 0.81s | out=1.2kB
  [tool] knowledge_agent  done in 2.41s | out=512c
  ```
- Hooks to implement / improve:
  - `on_chain_start` / `on_chain_end` / `on_chain_error` — DEBUG level,
    only log named chains (skip anonymous ones to reduce noise).
  - `on_tool_start` / `on_tool_end` / `on_tool_error` — INFO with summary,
    DEBUG with full input/output (truncated via `truncate_log`).
  - `on_llm_start` / `on_chat_model_start` / `on_llm_end` / `on_llm_error`
    — DEBUG. On end, extract token usage from `response.llm_output` and
    `AIMessage.usage_metadata` when present.
  - `on_agent_action` / `on_agent_finish` — DEBUG.
- **Timing**: per-run start time stored in the run map.
- **Memory**: at the end of each top-level run (depth 0), log peak RSS via
  `resource.getrusage(RUSAGE_SELF).ru_maxrss` (KB on Linux, bytes on
  macOS). Stdlib only, no new deps.

### 2. Wire the handler into the CLI

`cli.py` currently passes `callbacks=[]`. Instantiate
`ToolCallbackHandler()` (no transport) and register it so the CLI gets the
same trace stream as the bot.

### 3. Sub-agent visibility

Sub-agents are already exposed as `@tool`s in `agent_builder.py`, so the
tool callbacks fire for them automatically. The tool name is the
sub-agent name (`coding_agent`, `journal_agent`, ...). With depth-aware
indentation this gives us "main → sub-agent → tool" traces for free.

Bump the `logger.debug` line in `create_sub_agent`'s wrapper to also log
the response length on the way back, so we can see what each sub-agent
actually returned.

### 4. Output formatting

Use Rich markup in log messages (RichHandler is already configured) for
quick visual scanning:

- tool name → `[bold cyan]`
- duration → `[yellow]`
- errors → `[bold red]`
- depth indent → leading spaces

### 5. Don't change

- The existing `LOG_LEVEL` env var + `default_level` mechanism stays.
- Library loggers stay pinned at ERROR.
- Telegram bot keeps INFO default; CLI keeps DEBUG default.

## Acceptance criteria

- [x] Running the CLI shows tool calls, sub-agent calls, and LLM calls
      with depth-based indentation and durations.
- [x] Tool input and output payloads are visible at DEBUG (truncated).
- [x] Token usage is logged for LLM calls when the provider reports it.
- [x] Peak RSS is logged once per top-level user request.
- [x] Telegram bot output stays at INFO by default and isn't spammier
      than before for normal traffic.
- [x] No new runtime dependencies.

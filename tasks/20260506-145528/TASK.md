# Create a CLI tool for the bot that we can use instead of telegram

- STATUS: CLOSED
- PRIORITY: 100
- TAGS: feature,cli

## Goal

Build a simple REPL-style CLI tool that lets us chat with the Scufris agent
locally, without having to deploy the Telegram bot. This makes iteration on
prompts, tools, and agent behavior much faster during development.

## Design

### Entry point

- New file: `cli.py` at the repo root.
- Registered as a `pyproject.toml` script: `scufris-cli = "cli:main"`.
- Invoked via `uv run scufris-cli` (or `python cli.py`).

### Behavior

- Plain stdin/stdout REPL (NOT a full TUI). Uses Python's built-in `readline`
  module so we get line editing, history navigation (up/down arrows), and
  Ctrl+R search "for free" on Linux. No `rlwrap` wrapper required.
- Uses `rich` (already a project dependency) for colorized output:
  - User prompt label in cyan
  - Assistant responses rendered as markdown (rich.Markdown)
  - Tool invocations / status lines in dim yellow
  - Errors in red
- Reuses the existing pieces: `setup_scufris`, `create_agent_manager`,
  `create_history_manager`, `ToolCallbackHandler`. We keep a single fake
  user_id (e.g. `0`) for history scoping.
- Persists readline history to `~/.scufris_cli_history` so prompts survive
  across sessions.

### Special slash commands

Typed at the prompt, handled locally (NOT sent to the agent):

- `/help`        — show available commands
- `/clear`       — clear chat history for the CLI session
- `/history`     — print message count + history stats
- `/multiline`   — toggle multiline input mode (terminate input with a line
                   containing only `.` or EOF/Ctrl-D)
- `/exit`, `/quit`, or Ctrl-D on empty line — exit the REPL

### Configuration

The current `Config` class hard-fails if `TELEGRAM_BOT_TOKEN` or
`ALLOWED_USER_IDS` are missing. For the CLI we don't need either of these,
so we extend `load_config()` to accept a `require_telegram: bool = True`
flag and have the CLI call `load_config(require_telegram=False)`. Telegram
fields default to `None` / `[]` in that mode.

### Callback handler

`ToolCallbackHandler` accepts a `TelegramTransport` but, looking at the
implementation, it only uses it conceptually for typing actions (none of
the `on_tool_*` methods actually call into it today). We keep things simple
and just don't register the callback in the CLI path — tool activity is
already logged to the console via the `scufris-bot.agent.tools` logger and
will be visible at INFO level.

## Example session

```
$ uv run scufris-cli
Scufris CLI — type /help for commands, Ctrl-D to exit
> what's the weather in Bucharest?
[tool] duckduckgo_search ...
Assistant: It's currently 14°C and partly cloudy in Bucharest.
> /history
Messages in this session: 2
> /clear
Cleared 2 messages.
> /exit
bye!
```

## Acceptance criteria

- [x] `uv run scufris-cli` starts a REPL without needing TELEGRAM_BOT_TOKEN.
- [x] Messages are sent through the same agent pipeline as the Telegram bot.
- [x] History is preserved across turns within a session and can be cleared.
- [x] Slash commands (`/help`, `/clear`, `/history`, `/exit`) work.
- [x] Readline editing + arrow-key history works.
- [x] Existing telegram bot (`main.py`) is unaffected.

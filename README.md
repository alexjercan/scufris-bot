# Scufris

Scuffed Jarvis — a personal assistant bot powered by an Ollama-backed
LangChain agent hierarchy, exposed both as a Telegram bot and a local CLI.

## Running

### Telegram bot

Requires `TELEGRAM_BOT_TOKEN` and `ALLOWED_USER_IDS` in the environment
(or a `.env` file).

```bash
uv run scufris-bot
```

## Debugging

For local development you usually don't want to round-trip through Telegram
on every change. There's a REPL-style CLI that talks to the same agent
pipeline directly from your terminal.

### Starting the CLI

```bash
uv run scufris-cli
```

No Telegram credentials are needed — `load_config(require_telegram=False)`
skips that validation. You still need a working Ollama setup (configured
via `OLLAMA_MODEL`, `OLLAMA_BASE_URL`, etc., same as the bot).

### Using it

You get a prompt with line editing, arrow-key history (persisted to
`~/.scufris_cli_history`), and Ctrl-R search via `readline`. Assistant
replies are rendered as Markdown inside a green panel.

```
Scufris CLI — type /help for commands, Ctrl-D on empty line to exit.
> what's 2 + 2?
╭─ scufris ──────────────────────────────────────────────╮
│ 4                                                      │
╰────────────────────────────────────────────────────────╯
> /stats
Per-agent:
  agent       model         memory  calls  last
  ──────────  ────────────  ──────  ─────  ────
  scufris     qwen3:latest  2 msgs      1  0s ago
> /exit
bye!
```

### Slash commands

| Command      | What it does                                                |
| ------------ | ----------------------------------------------------------- |
| `/help`      | List available commands                                     |
| `/clear`     | Clear chat history for this session                         |
| `/stats`     | Per-agent memory + telemetry breakdown                      |
| `/multiline` | Toggle multiline input (end with a single `.` line)         |
| `/exit`, `/quit` | Exit the REPL (Ctrl-D on an empty line works too)       |

Anything that doesn't start with `/` is sent to the agent. Tool activity
is logged to stderr through the normal `scufris-bot.agent.tools` logger,
so you can watch what the agent is doing in real time.

### Multiline input

Toggle with `/multiline`, then type as many lines as you want and finish
with a line containing a single `.`:

```
> /multiline
multiline mode on — finish input with a single '.' line
> please summarize the following:
… line one
… line two
… .
```

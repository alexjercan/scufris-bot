# Surface agent thinking as chat messages in the CLI

- STATUS: CLOSED
- PRIORITY: 60
- TAGS: feature,cli,ux

## Motivation

Right now, when the user asks "what's the weather in Bucharest?" everything
between the prompt and the final answer is buried in DEBUG/INFO log lines
(`tool knowledge_agent start | ...`). For development that's great, but it
would be much nicer if the *user-visible* part of the CLI also showed a
"thinking" trail — short chat-style messages like:

```
> what's the weather in Bucharest?
  scufris is thinking…
  → asking knowledge_agent
  knowledge_agent: I'll use the weather tool for Bucharest
  → calling weather(Bucharest)
  ╭─ scufris ───────────────────────────────────────────╮
  │ The current weather in Bucharest is partly cloudy…  │
  ╰─────────────────────────────────────────────────────╯
```

Logs would still carry the full trace (with timings, RSS, tokens, etc.),
but the *primary* terminal output would feel like a real chat with a
visible "thinking" status — similar to how Claude / ChatGPT show
intermediate reasoning. In production (Telegram) the same hook can drive
typing actions or progress messages; we don't have to render those there.

## Design

### Hook in the callback handler

Extend `ToolCallbackHandler` (`utils/callbacks.py`) with optional
constructor arguments:

```python
ToolCallbackHandler(
    telegram_transport=None,
    update=None,
    on_thinking: Optional[Callable[[ThinkingEvent], None]] = None,
)
```

Where `ThinkingEvent` is a small dataclass:

```python
@dataclass
class ThinkingEvent:
    kind: Literal["text", "tool_call", "tool_result"]
    source: str        # e.g. "main", "knowledge_agent"
    text: str          # the message to display
    depth: int         # nesting level (for indentation/styling)
```

Emit events from:

- `on_llm_end` — if the resulting `AIMessage.content` (or any
  `additional_kwargs.reasoning_content` when `reasoning=True` on Ollama)
  is non-empty, emit a `text` event with that content. This captures the
  model's natural-language reasoning between tool calls.
- `on_tool_start` — emit a `tool_call` event with the tool name + a short
  preview of the input.
- `on_tool_end` — *optionally* emit a `tool_result` event when the output
  is small / informative (skip if it's >N chars to avoid spamming).

Default: `on_thinking=None` means no events are emitted (Telegram bot
behaviour unchanged).

### CLI integration

`cli.py` provides an `on_thinking` callback that uses Rich to render
each event in a dim, indented style above the final assistant panel:

```python
def render_thinking(ev: ThinkingEvent) -> None:
    indent = "  " * ev.depth
    if ev.kind == "tool_call":
        console.print(f"[dim]{indent}→ {ev.text}[/dim]")
    elif ev.kind == "text":
        console.print(f"[dim italic]{indent}{ev.source}: {ev.text}[/dim italic]")
    else:
        console.print(f"[dim]{indent}↩ {ev.text}[/dim]")
```

The final assistant reply still gets its green panel as today.

### Telegram integration (later, optional)

For the Telegram bot we keep `on_thinking=None` for now (or wire it to a
debounced "Scufris is typing…" status message). Out of scope for this
task — the goal here is the CLI experience.

### Throttling / noise control

- Skip empty `text` events.
- For `text` events, truncate to ~200 chars in the chat view (full text
  still goes to the log via the existing handler).
- Don't emit `tool_result` events by default — only `text` and
  `tool_call`. We can revisit if the trail feels too sparse.

## Acceptance criteria

- [x] `ToolCallbackHandler` accepts an `on_thinking` callback; default
      behaviour is unchanged when it's not provided.
- [x] CLI shows dim "thinking" lines (model reasoning + outgoing tool
      calls) above the final answer panel.
- [x] Indentation reflects nesting (main → sub-agent → tool).
- [x] Telegram bot output is unchanged.
- [x] Existing log trace still works at DEBUG level.

## Open questions

- For Ollama with `reasoning=True`, where does the reasoning content
  actually land on the AIMessage? `.content`, `additional_kwargs`, or
  somewhere else? Verify before implementing.
- Should `tool_call` events fire at every depth, or only at depth 0
  (main → sub-agent boundary)? Current proposal: every depth, indented.

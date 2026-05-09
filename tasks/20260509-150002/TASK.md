# Polish CLI thinking UI: display names, sub-agent context, styling

- STATUS: CLOSED
- PRIORITY: 55
- TAGS: feature,cli,ux,polish

## Issues found in the first pass

1. **Background colour on the thinking text.** Rendering the model's
   intermediate reasoning with `[dim italic]` makes some terminals
   (notably mine: kitty + tmux) draw a grey background block, which is
   ugly on a black terminal. We just want flat dim text.

2. **`source` is always "main".** When the main agent invokes a
   sub-agent tool (`knowledge_agent`), the sub-agent does its own
   `agent.invoke(...)` call inside the tool function — but that nested
   invoke doesn't inherit the outer callback context, so its LLM/tool
   runs come back with no `parent_run_id`. The handler therefore sees
   them as top-level and labels them `main`.

3. **Robotic tool-call line.** Today we print:

   ```
   → main calls knowledge_agent({'query': 'weather in Bucharest'})
   ```

   It would feel much more natural as:

   ```
   → Scufris asks the Knowledge Agent: weather in Bucharest
   ```

## Plan

### 1. Fix styling

- Replace `[dim italic]` with plain `[grey50]` (or `[bright_black]`) for
  the body text and a soft `[cyan]` for the source label. No italic.
- Drop the leading `→` for `text` events (keep it for `tool_call`).

### 2. Propagate callbacks into sub-agents

In `utils/agent_builder.py`, change the `sub_agent_tool` wrapper to
accept a `RunnableConfig` and pass it through:

```python
from langchain_core.runnables import RunnableConfig

@tool
def sub_agent_tool(query: str, config: RunnableConfig) -> str:
    ...
    response = agent.invoke(
        {"messages": [{"role": "user", "content": query}]},
        config=config,
    )
```

LangChain's `@tool` decorator injects the current `RunnableConfig` when
a parameter is annotated with that type. Passing it into the sub-agent
makes the sub-agent's runs children of the tool run, which means:
- `parent_run_id` chains work
- `_enclosing_tool_name()` correctly reports `knowledge_agent`
- depth indentation reflects real nesting

### 3. Human-friendly display names

Add a small registry in `utils/callbacks.py` (or a new
`utils/display.py`) mapping technical names to display names:

```python
DISPLAY_NAMES = {
    "main": "Scufris",
    "coding_agent": "Coding Agent",
    "knowledge_agent": "Knowledge Agent",
    "utilities_agent": "Utilities Agent",
    "journal_agent": "Journal Agent",
    "weather": "Weather",
    "web_search": "Web Search",
    "calculator_tool": "Calculator",
    "datetime_tool": "Date/Time",
    "opencode": "OpenCode",
    # journal tools — fall back to Title Case if missing
}

def display_name(technical: str) -> str:
    return DISPLAY_NAMES.get(technical, technical.replace("_", " ").title())
```

Apply it in:
- `ThinkingEvent.source` text rendering (CLI side — keep raw name in
  the event payload, format at render time).
- The tool-call message text.

### 4. Smarter tool-call message

In `on_tool_start`, build a friendlier string:

- Try to JSON-parse `input_str`. If it's a single-key dict (e.g.
  `{"query": "..."}` or `{"__arg1": "..."}` or `{"expression": "..."}`),
  use just the value as the "argument".
- Otherwise, pass the raw input through `truncate_log`.
- Render in CLI as `→ {source} asks {target}: {arg}` for sub-agents
  (depth 0 tool calls), or `→ {source} uses {target}: {arg}` for
  regular tools (deeper).

To know whether a tool is a sub-agent, we keep a known set:
`{"coding_agent", "knowledge_agent", "utilities_agent", "journal_agent"}`.
Belt-and-braces: a tool whose name ends in `_agent` is treated as a
sub-agent.

Carry the parsed argument in the `ThinkingEvent` so the renderer has
both the technical context and the friendly text. Concretely: add an
optional `arg` field to the event:

```python
@dataclass
class ThinkingEvent:
    kind: Literal["text", "tool_call", "tool_result"]
    source: str
    text: str          # short display label (target name for tool_call)
    depth: int
    arg: Optional[str] = None   # the human-meaningful argument, if any
```

The renderer in `cli.py` then composes the final sentence using
`display_name()` and `arg`.

## Acceptance criteria

- [x] Thinking text in CLI renders as flat dim text — no background
      block — on kitty + tmux.
- [x] When the main agent calls `knowledge_agent`, the LLM text events
      from inside the sub-agent are labelled `Knowledge Agent`, not
      `Scufris`/`main`.
- [x] Tool-call lines read like natural English, e.g.
      `→ Scufris asks Knowledge Agent: weather in Bucharest`.
- [x] Display names are easy to extend (single registry).
- [x] Telegram bot behaviour unchanged.

## Follow-up bug: source still reads "Scufris" for tools inside sub-agents

After the first fix, the *delegation* line correctly reads
`→ Scufris asks Knowledge Agent: weather in Ploiesti`, but the
nested call still reads `→ Scufris uses Weather: ...` instead of
`→ Knowledge Agent uses Weather: ...`.

### Root cause analysis

The expected run-tree:

```
main_graph (chain)
  main_llm (llm)
  knowledge_agent (tool)        <-- kind="tool", parent walk should land here
    sub_graph (chain)
      knowledge_llm (llm)
      weather (tool)
```

`_enclosing_tool_name(weather.parent_run_id)` walks up via
`_parents` looking for a registered run with `kind == "tool"`. It
returns "main" only if the walk reaches `None` without seeing one.

That can only happen if `weather`'s ancestry does NOT include
`knowledge_agent`. The most plausible reason: the `RunnableConfig`
injected by `@tool` into `sub_agent_tool` is the **outer** config
(the one used to invoke the tool). When the inner
`agent.invoke(..., config=config)` runs, its first run inherits
`parent_run_id` from that outer config — i.e. the parent of the
knowledge_agent tool itself, not knowledge_agent. So weather and
knowledge_agent end up as **siblings**, not parent/child.

Confirms: the delegation line is right because knowledge_agent IS
a child of the main graph; the inner tools just skip past
knowledge_agent in the parent chain.

### Fix

Use `run_manager: CallbackManagerForToolRun` (also auto-injected
when annotated on the @tool function) to obtain a child callback
manager whose parent is the current tool run, then pass it to the
inner invoke:

```python
from langchain_core.callbacks import CallbackManagerForToolRun

@tool
def sub_agent_tool(
    query: str,
    config: RunnableConfig,
    run_manager: CallbackManagerForToolRun,
) -> str:
    child_config = {**config, "callbacks": run_manager.get_child()}
    response = agent.invoke(
        {"messages": [{"role": "user", "content": query}]},
        config=child_config,
    )
```

`run_manager.get_child()` returns a CallbackManager seeded with
this tool's run as parent — so every run spawned by `agent.invoke`
will have `parent_run_id` pointing at knowledge_agent (directly or
transitively), making `_enclosing_tool_name` return the right name.

### Steps

1. Update `utils/agent_builder.py`'s `sub_agent_tool` signature and
   body as above.
2. Run `uv run ruff check`.
3. Smoke-test the CLI with a query that triggers a sub-agent's
   tool (e.g. "what's the weather in Ploiesti").
4. Verify the line reads `→ Knowledge Agent uses Weather: ...`.

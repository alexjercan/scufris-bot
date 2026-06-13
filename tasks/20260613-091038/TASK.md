# Port journal tools (today/daily/macros) as opencode tools

- STATUS: OPEN
- PRIORITY: 85
- TAGS: tools,journal,opencode

Wrap the existing `today` / `daily` / `macros` CLIs as
opencode-compatible tool definitions. Same subprocess approach as v1's
`utils/tools/journal_tools.py`; opencode tool schema instead of
LangChain `@tool`. Functional parity with v1.

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).


# scufris-server v2: HTTP daemon wrapping opencode serve

- STATUS: OPEN
- PRIORITY: 90
- TAGS: server,deploy,opencode

Single long-running process that owns opencode session lifecycle and
exposes our own `/v1/*` API (chat, stream, stats, clear) on top of
`opencode serve`. Same shape as v1's `scufris-server`, with opencode
replacing the in-process LangChain agent.

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).


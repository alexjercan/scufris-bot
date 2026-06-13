# SSE streaming of opencode 'thinking' events

- STATUS: OPEN
- PRIORITY: 75
- TAGS: server,observability

Bridge opencode's tool-call / reasoning stream into the same
`ThinkingEvent`-style SSE the v1 CLI already knows how to render. Lets
the existing CLI thinking-trace UX work unchanged against the v2
server.

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).


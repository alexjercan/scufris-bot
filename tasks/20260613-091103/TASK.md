# Server observability: request IDs, structured JSON logs, /metrics endpoint

- STATUS: OPEN
- PRIORITY: 50
- TAGS: server,observability

Port v1 task `20260513-121622` to the v2 server: `X-Request-ID`
middleware, JSON log formatter for systemd / CI, Prometheus
`/v1/metrics` endpoint. Same scope, retargeted at the opencode-backed
server. Added during planning as a v1-scope gap that wasn't in the
original 27-row backlog.

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).


# Spike: scufris-server-v2 performance baseline (latency, memory, throughput)

- STATUS: OPEN
- PRIORITY: 35
- TAGS: spike,performance,server

Establish a reproducible perf baseline for the v2 server, mirroring v1
task `20260513-121621`: bench harness against a stubbed opencode
endpoint, p50 / p95 latency, time-to-first-token, peak RSS, cold and
warm cache. Drives any later optimisation work. Added during planning
as a v1-scope gap that wasn't in the original 27-row backlog.

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).


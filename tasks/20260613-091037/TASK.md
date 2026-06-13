# Spike: per-(user, agent) session/context model on top of opencode

- STATUS: OPEN
- PRIORITY: 70
- TAGS: spike,memory,opencode

Figure out the opencode equivalent of v1's per-(user, agent) history +
compaction — does opencode persist sessions, for how long, and can we
reuse that instead of our own SQLite store? Output drives whether the
persistent conversation store and history-compaction tasks are needed
at all.

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).


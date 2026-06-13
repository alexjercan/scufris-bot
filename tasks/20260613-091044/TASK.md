# Per-user opencode session management (create/resume/expire)

- STATUS: OPEN
- PRIORITY: 85
- TAGS: server,sessions,memory

Map our `user_id` to opencode session IDs; handle creation, reuse, and
cleanup / expiry. Depends on the session-model spike settling whether
opencode's own session lifecycle is enough or whether we need a thin
mapping layer of our own.

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).


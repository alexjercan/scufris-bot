# Auth: bearer token for non-localhost binds

- STATUS: OPEN
- PRIORITY: 40
- TAGS: server,security

Same bearer-token story as v1 — required only for non-loopback
deployments, configured via env. Localhost stays unauthenticated for
v1, with the upgrade path to per-user tokens documented.

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).


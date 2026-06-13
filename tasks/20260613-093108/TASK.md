# Permissions UX bridge: opencode permission.asked → Telegram inline buttons

- STATUS: OPEN
- PRIORITY: 73
- TAGS: telegram,permissions,opencode

Bridge opencode's permission flow into the user-facing clients
(Telegram first, CLI second). When opencode emits a `permission.asked`
SSE event for a session we own, surface it to the corresponding chat
as an inline-keyboard prompt (allow / allow once / deny), then post
the user's reply back to
`POST /session/:id/permissions/:permID`.

Without this, every gated tool just stalls the agent loop until the
opencode-side timeout, so this is a hard prerequisite before any
destructive tool (`bash`, `edit`, `write`, `apply_patch`, custom
shell-spawning tools) is enabled in production.

Surfaced by the opencode runtime spike
(`tasks/20260613-091035/TASK.md`); depends on the Telegram bot v2
adapter (`tasks/20260613-091050/TASK.md`) and the SSE streaming work
(`tasks/20260613-091045/TASK.md`).

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).

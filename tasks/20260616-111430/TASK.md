# Stale opencode session_links auto-expiry

- STATUS: OPEN
- PRIORITY: 50
- TAGS: server,sessions,housekeeping

Drop `session_links` rows whose `last_used_at` is older than a
configurable TTL (default: 30 days). Pure housekeeping — opencode's
own sessions are *not* touched (scufris never destroys opencode
state; see ADR-8). Effect on the user: a long-idle channel's next
chat creates a fresh opencode session instead of resuming an
ancient one.

Mentioned in #10's task body ("create, reuse, cleanup / **expire**")
but no design existed. Split out during #10 scoping on 2026-06-16
because expiry is a housekeeping concern that doesn't gate the
list/clear surface and benefits from being its own small change.

---

## Scope

### In

1. New `Settings.SCUFRIS_SESSION_TTL_DAYS: int | None = 30`. `None`
   disables expiry entirely (opt-out). Validation: positive int or
   `None`.
2. New `scufris_server.sessions.prune_stale_links(conn, ttl_days)
   -> int` that deletes rows where
   `last_used_at < now - ttl_days * 86400` and returns the count.
3. Lifespan hook: call `prune_stale_links` once at startup (after
   migrations, before yielding control). Log a single INFO line
   with `extra={pruned_count, ttl_days, cutoff_ts}`.
4. Tests: unit test for the pruning function (insert old + fresh
   rows, assert only old ones dropped); lifespan test that
   `pruned_count` is logged.

### Out

| Feature                                      | Note                                |
|----------------------------------------------|-------------------------------------|
| Periodic background pruning (not just boot)  | Defer; boot-only is sufficient      |
|                                              | for v0 single-host single-user      |
| Per-user TTL overrides (`[user.session_ttl]`)| Speculative; revisit when needed    |
| Pruning the `channels` row too               | Out — channels are cheap, keeping   |
|                                              | them preserves the audit trail      |
| Pruning `event_log` rows                     | Separate housekeeping concern       |

---

## Design notes (sketched, lock when starting)

### Why boot-only vs periodic?

Single-host, single-user. Process restarts are routine (config
changes, deploys). A boot-time prune amortises cleanly. Adding a
periodic task (asyncio loop or APScheduler) is a non-trivial
lifecycle concern; not worth the complexity until we have a
multi-day-uptime deployment where staleness actually matters.

### Why keep `channels` rows?

The `channels` table is the audit trail of "which surfaces this user
has chatted from." Dropping rows there would lose history. Storage
cost is negligible (one row per channel ever used, ~80 bytes).
Pruning only the `session_links` row is sufficient — next chat to
that channel inserts a fresh link.

### TTL choice (30 days default)

Mirrors typical chat-history retention defaults. Opt-out via `None`
for users who want to never expire. Lower bound checked at startup
(positive int).

---

## Sketch — TODOs (fill in when starting)

1. Add `SCUFRIS_SESSION_TTL_DAYS` to `Settings` + `test_config.py`.
2. Add `prune_stale_links` to `scufris_server.sessions`.
3. Wire into lifespan after migrations.
4. Unit tests (function + lifespan log).
5. Update `docs/004_configuration.md` with the new env var.

---

## Dependencies

- **Hard-blocks:** #10 (`20260613-091044`) for the
  `scufris_server/sessions.py` module the prune fn lives in.
- **No soft-blocks.** Can land any time after #10.

Spun off from #10 scoping (`tasks/20260613-091044`).

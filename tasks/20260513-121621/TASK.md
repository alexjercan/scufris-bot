# Spike: scufris-server performance baseline (latency, memory, token throughput)

- STATUS: OPEN
- PRIORITY: 0
- TAGS: spike,performance,server

## Goal

Establish a reproducible performance baseline for `scufris-server` so
later optimisation work can be measured against numbers, not vibes.
Identify the top 1–2 hot spots worth fixing in this sprint, defer the
rest as follow-up tasks.

## Scope

### In
- Write a small `bench/` harness (Python script, no new framework)
  that:
  - Spins up `scufris-server` against a stubbed Ollama endpoint
    returning canned tokens at a configurable rate.
  - Drives N concurrent users issuing M turns each via
    `ScufrisClient`.
  - Records: end-to-end latency p50/p95/p99, time-to-first-token,
    tokens/sec, peak RSS, GC pause if any.
- Run the harness against:
  - Cold cache (fresh process, empty history).
  - Warm cache (after K turns of history per user, exercising the
    compactor).
- Profile one representative run with `py-spy` or `cProfile`; produce
  a flame graph + a one-page write-up of findings.
- Dump baseline numbers into `docs/perf-baseline.md` so future tasks
  can quote a "before" figure.

### Out
- Fixing anything found — file follow-up tasks instead.
- Bench against a real Ollama (model latency dominates and isn't
  what we're measuring).
- Multi-host / clustered scenarios.

## Acceptance criteria

- `bench/` script runs locally with `nix develop`, takes < 5 minutes
  end-to-end on a laptop.
- `docs/perf-baseline.md` lists at least: p50/p95 latency, TTFT,
  tokens/sec, peak RSS for both cold and warm-cache scenarios at
  C ∈ {1, 4, 16} concurrent users.
- A short "top findings" section names the 1–2 worst offenders
  (e.g. "history compaction blocks the event loop for X ms",
  "callback dispatch is O(handlers² · events)") and links to a
  follow-up task for each.

## Notes

- Stubbed Ollama is critical: real model inference dwarfs everything
  else and would drown out the signals we care about.
- Don't over-engineer the harness — a single Python file is fine.
- Token throughput here means our serialization / streaming overhead,
  not model gen rate.

## References

- `scufris_server/app.py`, `scufris_server/routes/chat.py`.
- `utils/memory_compactor.py` — async-but-may-be-blocking suspect.

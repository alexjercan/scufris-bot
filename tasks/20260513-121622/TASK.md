# Server observability: request IDs, structured JSON logs, /metrics endpoint

- STATUS: OPEN
- PRIORITY: 72
- TAGS: server,observability

## Goal

Make `scufris-server` debuggable in production: every request gets a
trace ID that flows through logs, every log line is structured JSON
when running under systemd, and a `/metrics` endpoint exposes
Prometheus-format counters/histograms for chat latency, errors, and
memory usage.

## Scope

### In
- Middleware that assigns each incoming HTTP request an `X-Request-ID`
  (honour an inbound one if present, else generate UUIDv7).
- Inject the request ID into a `contextvars.ContextVar` so every log
  line emitted during the request automatically carries it.
- Add a JSON log formatter alongside the current rich/console one.
  Default: console when stdin is a TTY, JSON otherwise (so systemd /
  CI / cloud loggers get parseable lines).
- Prometheus metrics via `prometheus-client` library exposed at
  `GET /v1/metrics` (no auth; safe-to-expose, but bind-restricted by
  default). Include:
  - `scufris_chat_requests_total{outcome}`
  - `scufris_chat_duration_seconds` (histogram)
  - `scufris_active_streams`
  - `scufris_history_compactions_total`
  - `scufris_process_resident_bytes`
- Update the NixOS module: optional `services.scufris.metrics.enable`
  toggle (default on; opens `/v1/metrics` on the same port).
- Tests: a fixture that hits a route and asserts the request ID
  appears in captured log records and the response header.

### Out
- OpenTelemetry / distributed tracing — overkill for v1.
- Per-tool metrics (separate task once the perf spike says where).
- Pushing metrics to a remote system; scraping is the consumer's
  problem.

## Acceptance criteria

- Every response carries an `X-Request-ID` header.
- Setting `SCUFRIS_LOG_FORMAT=json` emits one JSON object per log
  line containing `level`, `logger`, `msg`, `request_id`, `ts`.
- `curl http://127.0.0.1:8765/v1/metrics` returns Prometheus text
  format with at least the counters listed above.
- `nix flake check` still passes; the VM test additionally hits
  `/v1/metrics` and asserts a 200.

## Notes

- UUIDv7 is time-sortable and Python 3.13 has it built-in — no extra
  dep needed.
- Keep the JSON formatter dependency-free (stdlib `json` is fine).

## References

- `scufris_server/app.py`, `scufris_server/routes/admin.py`.
- `utils/logging.py` — current setup; extend, don't replace.

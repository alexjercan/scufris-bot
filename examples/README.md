# examples/

Small, runnable smoke scripts that exercise individual layers of
`scufris_server` from the outside — useful when verifying a step
landed cleanly, debugging a misconfigured env, or onboarding to the
codebase.

Each script is a single file, has a docstring at the top describing
Purpose / How to run / Expected output / Requires, exits non-zero on
failure, and cleans up after itself. Run from the repo root.

## Inventory

| Script                     | Purpose                                                          | Needs opencode? |
| -------------------------- | ---------------------------------------------------------------- | :-------------: |
| `check_schema.py`          | Apply migrations to a temp DB; list tables; verify idempotency.  |       no        |
| `check_settings.py`        | Print effective `Settings` and the env vars that fed them.       |       no        |
| `check_opencode_health.py` | Hit `GET /global/health` via `OpencodeClient`.                   |     **yes**     |
| `check_boot.py`            | Spawn `python -m scufris_server`; fetch `/openapi.json`.         |    optional     |
| `check_request.py`         | Boot the server; call `/v1/healthz` + `/v1/version`; show ids.   |    optional     |
| `check_chat.py`            | Boot the server; POST `/v1/chat` 3× to verify session reuse.     |     **yes**     |
| `check_sessions.py`        | Boot + 2 chats; walk list / clear / bulk-clear with idempotency. |     **yes**     |

## Quick start

From the repo root:

```sh
python examples/check_schema.py
python examples/check_settings.py
python examples/check_opencode_health.py    # requires `opencode serve` running
python examples/check_boot.py
python examples/check_request.py
python examples/check_chat.py                # requires opencode + a connected provider
python examples/check_sessions.py            # requires opencode + a connected provider
```

`check_boot.py` boots the server on an auto-picked free port (passed
in via `SCUFRIS_PORT`) so it never collides with a running dev
server. It does not require opencode — without it, the lifespan logs
a degraded-boot WARNING and the script still passes.

## Adding a new script

1. Name it `check_<thing>.py` — keep the prefix consistent.
2. Top-level docstring with `Purpose`, `How to run`, `Expected output`,
   `Requires` sections.
3. `def main() -> int:` returning 0 on success, non-zero on failure;
   wire `sys.exit(main())` (or `raise SystemExit(main())`) under
   `if __name__ == "__main__":`.
4. Tempfiles via `tempfile.TemporaryDirectory(prefix="scufris-...")`
   so leftovers don't accumulate.
5. Add a row to the table above.
6. Run `ruff check examples/<name>.py` and `ruff format examples/<name>.py`.

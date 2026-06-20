# 006 — Development

How to set up the dev environment, run tests, lint, type-check, and
pick up new tasks.

## Devshell

The repo is a Nix flake using `uv2nix`. Two ways to enter the
environment:

```bash
# Enter the devshell directly.
nix develop

# Or via direnv (if you have it installed).
direnv allow
```

Inside the shell you get:

- Python 3.13.12.
- `uv` 0.11.2.
- The full project venv on `$VIRTUAL_ENV`, already activated.
- `UV_NO_SYNC=1` set — uv won't try to manage its own venv.
- `REPO_ROOT` env var pointing at the repo root.

The venv is **read-only** (it lives under `/nix/store/`). To run
project commands against it use `uv run --active`:

```bash
uv run --active python -m scufris_server
uv run --active pytest
uv run --active mypy scufris_server tests
```

`--active` tells uv to use the already-activated environment instead
of trying to create a new one.

### Without Nix

If you can't or don't want to use Nix, the project is plain enough
that `uv sync` against `pyproject.toml` should work — but this isn't
the supported path and CI uses the flake.

```bash
uv sync
uv run pytest
```

## Project layout for new contributors

```
scufris-bot/
├── scufris_server/        # The v2 daemon. Live work happens here.
│   ├── app.py             # FastAPI factory + lifespan
│   ├── config.py          # Settings (env-driven)
│   ├── identity.py        # User resolver + TOML loader
│   ├── sessions.py        # Channel ↔ opencode-session service layer
│   ├── opencode_client.py # Async HTTP wrapper for opencode
│   ├── store.py           # SQLite + migrations
│   ├── logging.py         # JSON logging, request-id middleware
│   ├── dependencies.py    # FastAPI Depends() helpers
│   ├── events.py          # ThinkingEvent + per-process EventBus
│   ├── event_mapping.py   # Pure mapping opencode/event → ThinkingEvent
│   ├── sse.py             # SSE wire framing + keepalive helper
│   ├── routes/            # One APIRouter per endpoint group
│   ├── migrations/        # *.sql, applied at boot in lex order
│   └── internal/          # Reserved for plugin-only HTTP surface
├── scufris/               # v1 — legacy, mostly deleted stubs
├── tests/
│   ├── unit/              # Default `pytest` runs these — fully offline
│   └── integration/       # `pytest -m integration` — needs live opencode + ollama
├── tasks/                 # tatr task tracker — see "Task tracking"
├── examples/              # Small scripts that hit a live server
├── docs/                  # You are here
├── flake.nix              # Devshell + nix-build + nix flake check
└── pyproject.toml         # Deps, ruff, mypy, pytest config
```

## Tests

Two suites:

| Suite | Command | What it runs | Network? |
|-------|---------|--------------|----------|
| Unit | `uv run --active pytest` (or just `uv run --active pytest -m "not integration"`) | Everything in `tests/unit/`. ~290 tests. | No. opencode + ollama mocked via `respx`. |
| Integration | `uv run --active pytest -m integration` | Tests in `tests/integration/` (`test_chat_real.py`, `test_chat_stream_real.py`, `test_sessions_real.py`). | Yes. Needs live opencode + ollama with `qwen3:latest` pulled. |

### Unit tests

```bash
uv run --active pytest               # full suite, quiet output
uv run --active pytest -v            # verbose
uv run --active pytest tests/unit/test_chat_route.py  # one file
uv run --active pytest -k chat       # one keyword
uv run --active pytest -x            # stop at first failure
```

Conventions seen in the existing tests:

- One file per module: `test_<module>.py` for `<module>.py`.
- Test isolation via `tmp_path` for state dirs and `monkeypatch` for
  env vars. No globally-shared state.
- App tests construct a fresh app via `create_app(settings=Settings(...))`
  with overridden `state_dir` so the DB is per-test.
- opencode HTTP calls mocked with `respx.mock` against
  `OpencodeClient(base_url=...)`.
- Tests use `pytest.raises` for expected exceptions, `caplog.records`
  for log assertions (works against named loggers regardless of
  propagation — see the comment in `logging.setup_logging`).

If you're adding a new endpoint or module, mirror the existing
shape: write the test alongside the implementation, prefer
`monkeypatch.setenv` over manually shelling env vars.

### Integration tests

Integration tests exercise full round trips against live `opencode
serve` + `ollama` with `qwen3:latest`. They self-skip if either is
missing, so the suite stays green on CI where they aren't available.
Today there are three:

- `test_chat_real.py` — single-turn `/v1/chat` round trip.
- `test_chat_stream_real.py` — `/v1/chat/stream` SSE round trip.
  Two cases: a single turn that asserts at least one `thinking`
  event arrives plus a well-formed `done`, and a two-turn case that
  asserts session reuse across calls in the same channel. Both
  snapshot opencode's `/session` list before and after to verify
  the ADR-13 invariant (scufris never destroys upstream sessions).
- `test_sessions_real.py` — seed two channels, list with enrichment,
  clear one, list again, bulk clear, list again. Verifies the ADR-13
  "scufris doesn't destroy upstream sessions" invariant by snapshotting
  opencode's `GET /session` before and after each clear.

Prerequisites:

- `ollama serve` running, with `qwen3:latest` pulled via
  `ollama pull qwen3:latest`.
- `opencode serve` running on `OPENCODE_URL` (default
  `http://127.0.0.1:4096`) with at least `ollama` listed in
  `GET /provider`.

Invocation:

```bash
uv run --active pytest -m integration
```

Expected runtime: 20–40 s for chat, 30–60 s for sessions on a warm
cache; the first run after booting `ollama` can take longer while
the model loads. Per-test ceilings: 90 s for chat, 90 s for the
chat-stream happy path, 120 s for the chat-stream reuse case, and
120 s for sessions. Anything past those fails.

To exclude integration tests explicitly (CI default):

```bash
uv run --active pytest -m "not integration"
```

## Linting and type-checking

Both run as part of `nix flake check`. Run them locally first.

### Ruff

```bash
uv run --active ruff check .
uv run --active ruff format --check .
uv run --active ruff format .          # fix formatting in place
uv run --active ruff check --fix .     # fix safe lints in place
```

Configuration in `pyproject.toml` `[tool.ruff]`:

- Line length 88 (Black-compatible).
- Target `py313`.
- Selected rules: `E4 E7 E9 F B I Q` — pyflakes + a subset of
  pycodestyle, plus `B` (bugbear), `I` (isort), `Q` (quotes).
- `B` rules are unfixable on auto-`--fix` (catch-and-think rather
  than catch-and-rewrite).

### Mypy

```bash
uv run --active mypy scufris_server tests
```

Strict by default. Configured in `pyproject.toml` `[tool.mypy]`:

- `mypy_path = "."`
- Some third-party libs are tolerated without stubs (`dotenv`,
  `telegram`, `requests`, `uvicorn`) — see the `[[tool.mypy.overrides]]`
  blocks in `pyproject.toml`. Don't blanket-`# type: ignore`; instead
  add an override block for the package.

Type-hint conventions in the codebase:

- `from __future__ import annotations` at the top of every module
  (lazy evaluation of annotations, allows forward references).
- Pipe-syntax unions: `int | None`, `list[str]`. (3.10+, but we're
  on 3.13.)
- `Annotated[T, Depends(...)]` for FastAPI dependencies.
- pydantic `BaseModel` + `model_config = ConfigDict(extra=...)` for
  every wire-shape model.
- `NoReturn` for helpers that always raise (`_raise_503`).

### `nix flake check`

The full QA gate. Runs ruff, mypy, and pytest in isolated
derivations:

```bash
nix flake check
```

CI runs this. If it passes locally, it'll pass in CI (modulo
infrastructure flakes).

## Code conventions

These aren't formally enforced beyond ruff + mypy, but they're
visible across the codebase. Follow them for consistency.

### Docstrings

- Module-level docstring at the top of every `.py` file. Sets
  context, references the owning task, points at the design doc.
- Class-level docstring on every public class.
- Function-level docstring on every non-trivial function.
- Style is reST-flavoured prose, not Google-style. Sections like
  `Parameters`, `Raises`, `Behaviour` are pulled out with bullet
  lists.
- Comments inline when *why* matters. `# noqa: ...` annotations
  always carry a parenthetical explaining the rule.

### Imports

Always:

```python
from __future__ import annotations
```

Then standard library, third-party, first-party (separated by blank
lines, ordered by ruff's isort).

### Async vs sync

- Route handlers are `async def`.
- `get_db_conn` is `async def` deliberately — see `001_architecture.md`
  "Dependency injection".
- Pure-sync helpers (DB lookups, transformations) stay sync.
- The opencode client is fully async (`httpx.AsyncClient`).

### Error handling

- Exceptions are typed (custom hierarchy, e.g. `OpencodeError` →
  `OpencodeNetworkError` / `OpencodeServerError`).
- `try / except` is selective — catch the specific exception, not
  bare `except`.
- Operator-visible bugs (4xx from opencode, mis-seeded DB state)
  bubble up to FastAPI's 500 handler. Don't wrap them in our 503
  envelope.
- Network/5xx failures from opencode → 503 with structured body
  (`{error, error_type}`).

### Logging

- One JSON object per line via `JsonFormatter` (`logging.py`).
- Always include relevant fields as `extra={...}` kwargs:
  ```python
  logger.info("foo happened", extra={"user_id": uid, "channel": ...})
  ```
- INFO for first-time / interesting events.
- DEBUG for hot-path / cache-hit events that would otherwise drown
  the log.
- WARNING for degraded behaviour (opencode unreachable, default-model
  probe failed).
- The `request_id` field is automatic via `REQUEST_ID_VAR` —
  handlers don't need to thread it manually.

### Testing

- One test file per source module, mirroring the layout.
- `tmp_path` for filesystem isolation, `monkeypatch` for env vars,
  `caplog` for log assertions, `respx` for HTTP mocking.
- Test names: `test_<thing>_<expected_behaviour>` (e.g.
  `test_resolve_falls_back_to_default_user_on_unknown_surface_id`).

## Task tracking with `tatr`

The `tasks/` directory is managed by [`tatr`](file:///home/alex/.config/opencode/skills/tatr/SKILL.md), a CLI task tracker.
Each task lives at `tasks/YYYYMMDD-HHMMSS/TASK.md`.

Task states: `OPEN`, `IN_PROGRESS`, `CLOSED`.

Common commands:

```bash
tatr ls                        # list every task
tatr ls --sort priority        # by priority (high first)
tatr show <task-id>            # show one task's body
tatr new "Fix X" -p 50 -t feature,server
tatr close <task-id>           # mark CLOSED
```

The `<task-id>` is the directory name (`20260613-091046`). Use
`tatr ls` to find it.

When picking up new work:

1. Run `tatr ls --sort priority` to see what's open.
2. Read `tasks/<id>/TASK.md` end-to-end — most have rich
   acceptance criteria, open questions, and step-by-step TODOs.
3. Start in IN_PROGRESS, work through TODOs, mark each as CLOSED
   inline in the doc as you go.
4. When done, fill in the closing notes (test counts, deviations,
   follow-ups) and run `tatr` to mark CLOSED.

The current backlog (as of `#11` closing) — see `tasks/` directly
for authoritative state:

| Task | Priority | Status | Subject |
|------|----------|--------|---------|
| 091038       | 85 | OPEN | Journal tooling |
| 091049 (#14) | 80 | OPEN | scufris-cli v2 |
| 091039       | 75 | OPEN | Weather / web search tools |
| 091047 (#13) | 75 | OPEN | `/v1/stats` and `/v1/clear` |
| 093108 (#30) | 70 | OPEN | Permissions UX bridge |
| 111428 (#33) | 70 | OPEN | Channel fork |
| 111430 (#34) | 50 | OPEN | Channel server-side expire |

## Git workflow

The current development branch is `feature/opencode-v2`. Master is
the v1 line; v2 is being built up on the branch and will become
master once the migration is complete (design doc §18).

When making changes:

- Branch off `feature/opencode-v2`, not `master`.
- Commit messages: short imperative summary line, optional body.
- One concept per commit. Don't bundle a refactor with a feature.
- Run `nix flake check` before pushing. If it fails locally, it'll
  fail in CI.
- Push triggers the GitHub Actions CI workflow (`.github/workflows/ci.yml`
  — runs the same `nix flake check`).

PRs aren't part of the v2 carryover workflow today (single-developer
project) — direct push to the feature branch is the convention.

## Adding a new endpoint

The pattern (worked through several times for `routes/health.py`,
`routes/identity.py`, `routes/chat.py`):

1. **Create the router file** at `scufris_server/routes/<name>.py`.
   Define `router = APIRouter(prefix="/v1/<name>", tags=["<name>"])`
   at module level.
2. **Define the request / response models** as pydantic `BaseModel`
   classes in the same file. Use `Field(..., min_length=1, ...)` for
   non-empty validation.
3. **Implement the handler.** Use `Annotated[T, Depends(...)]` for
   every dependency. Keep handlers thin — push real logic into
   pure-Python helpers in a sibling module so they're independently
   testable.
4. **Mount the router.** Add `router as <name>_router` to the
   imports in `scufris_server/routes/__init__.py`, then append it to
   the `ROUTERS` list.
5. **Write tests.** New file `tests/unit/test_<name>_route.py`.
   Mirror the existing route tests' structure: app fixtures with
   isolated state, mocked opencode via `respx`, log assertions via
   `caplog`.
6. **Update `tests/unit/test_placeholders.py`** if the new endpoint
   replaces a placeholder. Each placeholder has corresponding
   "ROUTERS count" assertions and "module is empty" assertions that
   need to be updated.
7. **Update `docs/002_api_reference.md`** with the new endpoint's
   field-by-field shape.

## Adding a new SQLite migration

See [`005_data_model.md`](005_data_model.md) "Adding a new migration"
for the full procedure. Short version:

1. Create `scufris_server/migrations/00N_<description>.sql`.
2. Use forward-only DDL (`ADD COLUMN` over `RENAME`).
3. Don't edit any already-applied migration in place.
4. Add a test exercising the new tables / columns.
5. Boot the server once locally to apply the migration; check
   `_schema_migrations` for the new row.

## Common debugging recipes

### Server logs

JSON, one event per line. Pipe through `jq`:

```bash
uv run --active python -m scufris_server 2>&1 | jq -c .
```

Filter by level:

```bash
uv run --active python -m scufris_server 2>&1 | jq -c 'select(.level == "WARNING")'
```

Follow a single request through:

```bash
uv run --active python -m scufris_server 2>&1 | \
  jq -c 'select(.request_id == "01J...")'
```

### Inspect SQLite state

There's no `sqlite3` CLI in the devshell. Use Python:

```bash
uv run --active python - <<'PY'
import sqlite3
db = sqlite3.connect("/home/alex/.local/state/scufris/scufris.sqlite")
db.row_factory = sqlite3.Row
for r in db.execute("SELECT * FROM users"):
    print(dict(r))
PY
```

### Reset state for a clean run

```bash
rm -rf ~/.local/state/scufris
uv run --active python -m scufris_server
```

Migrations re-run. Default user re-seeded. No leftover bindings.

### See exactly what `/v1/chat` is sending opencode

Opencode logs every incoming request. Tail its log alongside:

```bash
opencode serve  # in terminal A — its logs are stdout
```

Or attach to opencode's `/event` SSE stream from a third terminal:

```bash
curl -N http://127.0.0.1:4096/event
```

## Cross-references

- The design doc: `tasks/20260613-091036/TASK.md`. The single source of
  architectural truth for v2. Long but worth reading once.
- The original v1 identity design: `tasks/20260520-145231/TASK.md`.
  Preserved for reference; v2 carries the TOML shape forward verbatim.
- Architecture overview: [`001_architecture.md`](001_architecture.md).
- API surface: [`002_api_reference.md`](002_api_reference.md).
- Identity layer: [`003_identity_and_users.md`](003_identity_and_users.md).
- Configuration: [`004_configuration.md`](004_configuration.md).
- Data model: [`005_data_model.md`](005_data_model.md).

# Plugin ↔ scufris-server protocol: facts HTTP contract + auth token model

- STATUS: OPEN
- PRIORITY: 60
- TAGS: opencode,plugin,facts,security

Lock down the loopback HTTP contract between the in-tree opencode
plugin (`.opencode/plugin/scufris.ts`) and `scufris-server`. The
plugin needs to read/write per-user facts on behalf of the agent, and
inject context at compaction time. Both sides need a stable shape and
an auth model that doesn't share scufris-server's user-facing
`SCUFRIS_TOKEN`.

Scope:

1. **Endpoints.** Sketch from the design doc:
   - `POST /v1/internal/facts` — body `{user_id, key, value}`;
     upsert. Idempotent.
   - `DELETE /v1/internal/facts/{key}?user_id=...` — remove.
   - `GET /v1/internal/facts?user_id=...` — list.
   - `GET /v1/internal/user/{user_id}/context` — facts + channel
     metadata, called from the `experimental.session.compacting`
     hook. Returns a compact JSON the plugin can splice into the
     compaction prompt.
2. **Auth.** `SCUFRIS_PLUGIN_TOKEN` separate from `SCUFRIS_TOKEN`.
   Bearer-header check on every `/v1/internal/*` call. Document
   rotation (both processes must restart). Endpoints under
   `/v1/internal/*` MUST 404 / 401 when called without the plugin
   token, even on loopback, to keep the user-facing API and the
   internal one disjoint.
3. **Error / retry semantics.** What does the plugin do on 5xx from
   scufris-server? Defaults: `remember` retries with backoff and
   surfaces a tool error if exhausted; the compaction hook fails
   open (compaction proceeds without injected context) and logs.
4. **Schema validation.** scufris-server validates request bodies via
   pydantic; plugin uses zod (or the equivalent already in opencode's
   plugin types).

Acceptance:
- `/v1/internal/*` route module in `scufris_server`, gated by plugin
  token.
- Pydantic models for the request/response shapes published as the
  source of truth; the TS plugin imports its types from a generated
  or hand-mirrored file (decide which).
- One end-to-end test: plugin (in opencode) calls `remember`, the
  fact appears in the next session's compaction summary.

Depends on #19 (`20260613-091053`, facts store) and #31
(`20260613-093911`, plugin scaffolding). Surfaced while writing the
v2 architecture design doc (`tasks/20260613-091036/TASK.md` — see
§5.3, §10.2, §14, §16.4, §17).

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).

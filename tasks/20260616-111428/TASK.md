# Per-channel session fork endpoint

- STATUS: OPEN
- PRIORITY: 70
- TAGS: server,sessions,fork

Implements design §8 line 295 / §9.4 / ADR-13: `POST
/v1/sessions/:channel_id/fork` creates a new opencode session branched
from the channel's current session **and** a new `channels` row
pointing at it. Original channel is unchanged — fork is *additive*,
git-branch-style.

Spun off from #10 (`tasks/20260613-091044`) during scoping on
2026-06-16: #10's task body names only create/resume/expire, and
fork carries a non-trivial channel-identity design question (D1
below). Split out so #10 can ship the list/clear surface cleanly.

---

## Scope

### In

1. New `OpencodeClient.fork_session(session_id, message_id=None)
   -> Session` wrapping `POST /session/{id}/fork {messageID?: "msg..."}`
   (live endpoint verified 2026-06-16; opencode 1.15.13 returns the
   new `Session` payload).
2. New `scufris_server.sessions.fork_channel(conn, oc_client,
   parent_channel_id, at_message_id=None) -> (new_channel_id,
   new_oc_session_id)`. Allocates a new `channels` row with a
   suffixed `surface_id` (see D1) + a fresh `session_links` row.
3. `POST /v1/sessions/:channel_id/fork` route in
   `scufris_server.routes.sessions`. Body: `{at_message_id?: str}`;
   response: `{channel_id: int, oc_session_id: str, surface_id: str}`
   so the client knows how to address the new channel for follow-up
   chats.
4. Unit tests (route + service) + 1 integration test driving a real
   fork against live opencode and verifying both channels remain
   independently chattable.

### Out

| Feature                            | Owner / note                                |
|------------------------------------|---------------------------------------------|
| Client UX (slash command, prompts) | #14 (CLI v2). Fork's HTTP API is decoupled. |
| Revert (`POST /session/:id/revert`)| ADR-13 — deferred until confirmation UX     |
| List-children traversal            | Out of v0 — opencode exposes children but   |
|                                    | we don't surface tree topology yet          |
| Fork with channel descriptor body  | D1 rejected option (b); revisit if needed   |

---

## Design decisions (locked during #10 scoping, 2026-06-16)

### D1: New channel's surface_id

**Locked: server-mints suffix.**

The new channel's triple is
`(user_id, parent.surface, parent.surface_id + "#" + new_oc_id[:8], parent.agent)`.
Deterministic, no schema migration, the suffix is short enough to
type, and the prefix preserves the parent's surface_id so clients can
visually group forks.

Example: parent `(cli, term-1, build)` forked yields
`(cli, term-1#9a4e0b2c, build)`.

Rejected alternatives:

- **(b) Client-supplied channel descriptor.** Fork body grows
  `{at_message_id?, channel?: {surface_id, agent?}}`. More flexible
  but pushes naming policy to every client and risks collisions
  when two clients race.
- **(c) Synthetic `surface="fork"`.** Forks become their own surface
  namespace. Clean conceptually but breaks the "channels list shows
  everything a user has" UX in #10's `GET /v1/sessions`.
- **(d) Schema change.** Add `parent_channel_id` + drop the
  `(user_id, surface, surface_id, agent)` UNIQUE. Cleanest model
  but wider blast radius and a migration we can avoid.

### D2: Fork inherits parent's agent

Body does not accept an `agent` override. If the user wants a
different agent they should start a fresh channel, not fork. Keeps
fork semantics: "branch this conversation at message X" — agent
identity is part of the conversation.

### D3: at_message_id validation

Pass through to opencode unchanged. If opencode 400s (invalid
message_id, doesn't belong to session), surface as
`OpencodeClientError` → FastAPI 500 (our request was malformed:
caller gave us a bogus id). If we want to be friendly and pre-validate
against `GET /session/{id}/message`, defer to a follow-up — adds a
round-trip and #14 can validate client-side once it knows the
session's message history.

---

## Sketch — TODOs (fill in when starting)

1. Extend `OpencodeClient`: add `fork_session(id, message_id?)`.
2. Add `sessions.fork_channel` service function.
3. Wire `POST /v1/sessions/:channel_id/fork` route.
4. Unit tests for route + service.
5. Integration test: real fork end-to-end.
6. Update `docs/002_api_reference.md`.

---

## Dependencies

- **Hard-blocks:** #10 (`20260613-091044`) for the
  `scufris_server/sessions.py` module + the list/clear surface
  this slots into.
- **Soft-blocks (UX):** #14 (`20260613-091049`, CLI v2). Fork has
  no driver until the CLI grows a `/fork` slash command. The HTTP
  API can land before the CLI and live untested-from-user POV in
  the interim.

Spun off from #10 scoping (`tasks/20260613-091044`).
Surfaced by scufris-v2 design doc (`tasks/20260613-091036/TASK.md`
§9.4, ADR-13).

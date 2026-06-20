# Verify SSE bus replay-on-reconnect (§16.1) and decide whether to add replay logic

- STATUS: OPEN
- PRIORITY: 40
- TAGS: followup,server,sse

## Why

`#11` (`tasks/20260613-091045`) shipped the per-process `EventBus`
with reconnect-on-drop using exponential backoff (1s → 30s cap). It
deliberately did *not* attempt to replay events emitted by opencode
during a disconnect — when a reconnect succeeds, every live
subscriber gets a `_BusReconnected` sentinel and the chat-stream
handler surfaces that as an `error` SSE event with
`error_type: "BusReconnected"`.

The design doc (`tasks/20260613-091036/TASK.md` §16.1) flagged
empirical verification as deferred to `#11`'s acceptance tests, but
in practice `#11` only proved the bus reconnects — it did *not*
exercise a drop *during* a live `/v1/chat/stream` turn against real
opencode + real ollama. This task is that empirical verification,
plus the design decision that comes out of it.

## What to do

1. **Reproduce the drop.** Spawn opencode + scufris-server normally,
   start a `/v1/chat/stream` turn against a slow-enough prompt (a
   multi-tool subagent spawn is ideal so the turn lasts > 1s).
   Mid-turn, kill the upstream `GET /event` connection — either by
   restarting `opencode serve`, or by reaching into the
   `EventBus._stream` task and forcibly cancelling it.

2. **Observe what's missed.** Count the events the chat-stream
   handler received before the disconnect; compare to what opencode
   *would have* emitted (poll `GET /session/{id}/message` after the
   turn finishes upstream — opencode persists the message even if
   the SSE consumer was disconnected). Quantify the loss: is it
   "the last 0.5s of deltas" or "an entire subagent fan-out"?

3. **Decide the policy.**
   - **Option A — accept the loss.** Keep the current
     `error_type: "BusReconnected"` surface; expect clients to retry
     the turn (which is safe because `/v1/chat` is also idempotent
     within a channel). Document the contract.
   - **Option B — replay on reconnect.** On reconnect, poll
     `GET /session/{id}/message` for every session with live
     subscribers and back-fill the missing events. Materially
     more code; harder to get right (deduplication, ordering, the
     "did this event already arrive?" check).
   - **Option C — best-effort heuristic.** Replay only the final
     assistant message body via `GET /session/{id}/message`, emit
     it as one synthetic `done` event with `tokens` / `cost`.
     Skip intermediate events. Hybrid that gives users a complete
     reply without the engineering surface of full replay.

4. **Implement the chosen option.** If A, just clarify the docs
   (`002_api_reference.md` `error` table + `001_architecture.md`
   "SSE event bus" section). If B or C, code lives in
   `scufris_server/events.py` plus a new opencode client call
   (`get_session_messages(session_id)` — already partially exists).

## Acceptance

- [ ] Reproduction recipe documented (one paragraph in this task,
      enough that a future contributor can hit the failure mode).
- [ ] Quantified loss measurement (e.g. "median 4 missed events
      over a 2s drop in a subagent-heavy turn").
- [ ] Decision recorded (A / B / C) with rationale.
- [ ] If A: docs updated. If B or C: code + tests + integration
      test that exercises the drop.

## Pointers

- `scufris_server/events.py:392` — `EventBus._reader` loop, where
  reconnect happens.
- `scufris_server/events.py:48` (import) — `_BusReconnected` sentinel.
- `scufris_server/routes/chat_stream.py` — how the sentinel is
  surfaced today.
- `tasks/20260613-091036/TASK.md` §16.1 — the original deferral.
- `tasks/20260613-091045/TASK.md` — `#11`, the bus implementation.

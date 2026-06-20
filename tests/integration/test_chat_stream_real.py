"""End-to-end /v1/chat/stream against real opencode + ollama (step 11 of #11).

Mirrors :mod:`tests.integration.test_chat_real`'s rationale: a real
opencode + ollama round-trip is the slowest thing in the suite, so we
keep the surface coverage tight — two tests that together prove the
SSE path delivers what the design doc asks for.

Sequence
--------
**Test 1 — happy path.** One ``POST /v1/chat/stream`` to a fresh
channel. Drains the SSE stream and asserts:

- HTTP 200 + ``Content-Type: text/event-stream``.
- ≥1 ``thinking`` event (proves the bus → mapper → route plumbing
  forwards opencode reasoning/text deltas — covers the "see what
  scufris is doing" UX ask in TASK.md §1).
- Exactly one terminal ``done`` event, zero ``error`` events.
- ``done`` payload shape: ``message`` non-empty, ``oc_session_id``
  startswith ``ses_``, ``oc_message_id`` startswith ``msg_``,
  ``tokens.input > 0``, ``tokens.output > 0``, ``cost >= 0``.
- **ADR-13 leak check**: opencode's ``/session`` list grows by
  exactly one over the test.

**Test 2 — session reuse.** Two ``POST /v1/chat/stream`` calls on
the same channel back-to-back. Asserts:

- Both reach a ``done`` event.
- Second turn's ``done.oc_session_id`` equals the first's.
- **ADR-13 leak check**: opencode's ``/session`` list grows by
  exactly one (one session shared by two turns), confirming the
  ``session_links`` reuse path drives both chat *and* chat-stream.

We deliberately skip a "distinct channel" subtest — that's already
covered by :mod:`test_chat_real`'s third POST and the routing logic
is identical between ``/v1/chat`` and ``/v1/chat/stream`` (both
call :func:`_resolve_session` from :mod:`routes.chat`). The
streaming-specific surface only adds the SSE wire format and the
event-bus drain loop on top.

Tool-call assertions are out of scope here (tool registration is
#28+); the happy-path existence of ``thinking`` events doubles as a
subagent-event smoke test per TASK.md step 11.

Activated with ``-m integration``; conftest fixtures skip the whole
module if opencode or its default model is unreachable, so
``pytest`` (no marker) stays green on CI without these services.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from scufris_server.app import create_app
from scufris_server.config import Settings
from scufris_server.opencode_client import ModelRef

# Per-test wall-clock ceilings. Matches :mod:`test_chat_real`'s 90s
# choice — qwen3 cold-start on a fresh ollama plus the bus
# subscription handshake comfortably fit, but a genuine hang still
# trips. Test 2 has two chats so it gets a bigger budget like
# :mod:`test_sessions_real` (120s for two cold chats + cheap calls).
WALL_CLOCK_BUDGET_S_HAPPY = 90.0
WALL_CLOCK_BUDGET_S_REUSE = 120.0

CHANNEL_HAPPY = {
    "surface": "integration",
    "surface_id": "stream_happy",
    "agent": "build",
}
CHANNEL_REUSE = {
    "surface": "integration",
    "surface_id": "stream_reuse",
    "agent": "build",
}


def _opencode_session_ids(opencode_url: str) -> set[str]:
    """Fetch opencode's ``GET /session`` list and return the set of ids.

    Side-channel verification — we hit opencode directly (not through
    scufris) so we can prove ADR-13: no scufris path destroys an
    upstream opencode session. The chat-stream path adds new
    sessions on first turn (same as ``/v1/chat``) and *never*
    deletes them.

    Lifted verbatim from :mod:`test_sessions_real` to avoid a tiny
    shared helper module for two callers. If a third caller appears,
    promote to ``tests/integration/_opencode.py``.

    Auth: matches what ``conftest.opencode_url`` already validated —
    if opencode required ``OPENCODE_SERVER_PASSWORD`` for
    ``/global/health``, the conftest probe would have skipped the
    module already, so we can assume an unauthenticated GET works
    here.
    """
    resp = httpx.get(f"{opencode_url}/session", timeout=5.0)
    resp.raise_for_status()
    body: list[dict[str, Any]] = resp.json()
    return {entry["id"] for entry in body}


def _drain_sse(
    client: TestClient, payload: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str]:
    """POST ``/v1/chat/stream`` and drain the SSE response.

    Returns ``(thinkings, dones, errors, content_type)``. Each list
    holds the parsed JSON payload of every event seen, in arrival
    order. Comment lines (``: keepalive``) are dropped. Unknown
    event names are surfaced via the ``errors`` bucket so a future
    server-side schema drift fails loudly rather than silently
    dropping data.

    Uses the test-client's ``stream()`` context manager so the
    ASGI lifespan + event-bus subscription stay alive for the full
    iteration. ``iter_lines()`` returns universal-newline-split
    strings; the SSE blank-line record terminator surfaces as an
    empty string in the iterator (matches the wire shape produced
    by :func:`scufris_server.sse.format_event`).
    """
    thinkings: list[dict[str, Any]] = []
    dones: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    content_type = ""
    with client.stream(
        "POST", "/v1/chat/stream", json=payload, timeout=WALL_CLOCK_BUDGET_S_REUSE
    ) as resp:
        assert resp.status_code == 200, resp.read().decode("utf-8", errors="replace")
        content_type = resp.headers.get("content-type", "")
        event_name = ""
        data_buf: list[str] = []
        for raw_line in resp.iter_lines():
            line = raw_line.rstrip("\r")
            if line == "":
                if event_name or data_buf:
                    parsed: dict[str, Any] = (
                        json.loads("\n".join(data_buf)) if data_buf else {}
                    )
                    if event_name == "thinking":
                        thinkings.append(parsed)
                    elif event_name == "done":
                        dones.append(parsed)
                    elif event_name == "error":
                        errors.append(parsed)
                    else:
                        # Unknown event — bin it as an error so the
                        # asserts below catch it. Forward-compatible
                        # event types added in future tasks should
                        # update this drain helper.
                        errors.append(
                            {"_unknown_event": event_name, "_payload": parsed}
                        )
                event_name = ""
                data_buf = []
                continue
            if line.startswith(":"):
                # SSE comment — drops keepalives. We don't assert on
                # keepalive presence here; the unit suite covers that
                # at the framing layer (``tests/unit/test_sse.py``).
                continue
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
                continue
            if line.startswith("data:"):
                # SSE spec: a single leading space after ``data:`` is
                # part of the prefix. Our server emits exactly that.
                data_buf.append(line[len("data:") :].lstrip(" "))
                continue
            # Unknown SSE field — ignore per spec.
    return thinkings, dones, errors, content_type


@pytest.mark.integration
def test_chat_stream_real_happy_path(
    tmp_path: Path,
    opencode_url: str,
    ollama_default_model: ModelRef,
) -> None:
    """One stream POST to a fresh channel; verify wire shape end-to-end.

    See the module docstring for the assertion list. The ADR-13 leak
    check brackets the test with two ``/session`` snapshots — the
    delta must be exactly +1 (the newly created session for this
    channel). If opencode is already populated with other sessions
    from prior runs, we filter by the new ``oc_session_id`` rather
    than asserting on the raw count, so concurrent state on the
    upstream doesn't make the test flaky.
    """
    settings = Settings(state_dir=tmp_path, opencode_url=opencode_url)
    app = create_app(settings)

    sessions_before = _opencode_session_ids(opencode_url)
    started_at = time.monotonic()

    with TestClient(app) as client:
        thinkings, dones, errors, ctype = _drain_sse(
            client,
            {
                "message": "reply with the single word: pong",
                "channel": CHANNEL_HAPPY,
            },
        )

    elapsed = time.monotonic() - started_at
    assert elapsed < WALL_CLOCK_BUDGET_S_HAPPY, (
        f"happy-path stream took {elapsed:.1f}s "
        f"(budget {WALL_CLOCK_BUDGET_S_HAPPY:.0f}s) — investigate"
    )

    # ---- Wire-shape assertions ----
    assert ctype.startswith("text/event-stream"), f"unexpected content-type: {ctype!r}"
    assert errors == [], f"stream emitted unexpected error events: {errors!r}"
    assert len(dones) == 1, (
        f"expected exactly one done event, got {len(dones)}: {dones!r}"
    )
    assert thinkings, "expected at least one thinking event (subagent / text deltas)"

    # ---- done payload ----
    done = dones[0]
    assert done.get("type") == "done", f"missing or wrong type field: {done!r}"
    assert done.get("message"), f"done.message must be non-empty: {done!r}"
    assert done.get("oc_session_id", "").startswith("ses_"), (
        f"oc_session_id missing or malformed: {done!r}"
    )
    assert done.get("oc_message_id", "").startswith("msg_"), (
        f"oc_message_id missing or malformed: {done!r}"
    )
    tokens = done.get("tokens") or {}
    assert tokens.get("input", 0) > 0, f"tokens.input must be > 0: {done!r}"
    assert tokens.get("output", 0) > 0, f"tokens.output must be > 0: {done!r}"
    # ``cost`` may legitimately be 0.0 for ollama (local model, no $).
    assert isinstance(done.get("cost"), int | float), f"cost must be a number: {done!r}"
    assert done["cost"] >= 0

    # ---- thinking payload sanity ----
    # Don't assert on a specific kind (which is provider/prompt-
    # dependent) — just that every event carries the
    # ``ThinkingEvent.to_payload`` shape produced by
    # :mod:`scufris_server.event_mapping`.
    for th in thinkings:
        assert th.get("kind"), f"thinking event missing kind: {th!r}"
        assert th.get("source"), f"thinking event missing source: {th!r}"
        assert isinstance(th.get("depth"), int), (
            f"thinking event missing/invalid depth: {th!r}"
        )

    # ---- ADR-13 leak check ----
    sessions_after = _opencode_session_ids(opencode_url)
    new_sessions = sessions_after - sessions_before
    assert done["oc_session_id"] in new_sessions, (
        f"the stream's oc_session_id should appear in opencode's "
        f"session list (ADR-13 — sessions live in opencode); "
        f"new={new_sessions!r}, expected={done['oc_session_id']!r}"
    )
    # Exactly one new session — the chat-stream path must not allocate
    # secondary sessions (no fork, no retry doubling). Filtering by
    # ``new_sessions`` insulates against concurrent activity on the
    # upstream opencode (e.g. another test or dev session running in
    # parallel) that would corrupt a raw count delta.
    assert new_sessions == {done["oc_session_id"]}, (
        f"chat-stream should allocate exactly one opencode session; "
        f"saw delta={new_sessions!r}"
    )


@pytest.mark.integration
def test_chat_stream_real_reuses_session_across_turns(
    tmp_path: Path,
    opencode_url: str,
    ollama_default_model: ModelRef,
) -> None:
    """Two stream POSTs on the same channel; second must reuse the session.

    The reuse path is the same code as ``/v1/chat`` (both delegate to
    :func:`routes.chat._resolve_session` + :func:`_touch_session`) —
    we still exercise it through the streaming entry point because a
    silent regression in the chat-stream handler's session-resolution
    branch wouldn't show up in :mod:`test_chat_real`.

    The ADR-13 leak check is the strongest assertion here: two POSTs
    on the same channel must result in exactly one new opencode
    session over the test's lifetime. Anything else means the
    handler took the create-fresh branch on the second turn (the
    relink-after-clear bug discovered during step 12 would surface
    differently — a UNIQUE-constraint 500 — but a future "always
    create" regression would silently double the session count).
    """
    settings = Settings(state_dir=tmp_path, opencode_url=opencode_url)
    app = create_app(settings)

    sessions_before = _opencode_session_ids(opencode_url)
    started_at = time.monotonic()

    with TestClient(app) as client:
        # ---- Turn 1: fresh channel ----
        _, dones_1, errors_1, _ = _drain_sse(
            client,
            {
                "message": "reply with the single word: pong",
                "channel": CHANNEL_REUSE,
            },
        )
        assert errors_1 == [], f"turn 1 emitted error events: {errors_1!r}"
        assert len(dones_1) == 1, f"turn 1 expected 1 done, got {dones_1!r}"
        session_first = dones_1[0]["oc_session_id"]
        assert session_first.startswith("ses_")

        # ---- Turn 2: same channel ----
        _, dones_2, errors_2, _ = _drain_sse(
            client,
            {
                "message": "reply with the single word: ack",
                "channel": CHANNEL_REUSE,
            },
        )
        assert errors_2 == [], f"turn 2 emitted error events: {errors_2!r}"
        assert len(dones_2) == 1, f"turn 2 expected 1 done, got {dones_2!r}"
        session_second = dones_2[0]["oc_session_id"]
        assert session_second == session_first, (
            f"same channel must reuse opencode session: "
            f"{session_second!r} != {session_first!r}"
        )

    elapsed = time.monotonic() - started_at
    assert elapsed < WALL_CLOCK_BUDGET_S_REUSE, (
        f"reuse stream took {elapsed:.1f}s "
        f"(budget {WALL_CLOCK_BUDGET_S_REUSE:.0f}s) — investigate"
    )

    # ---- ADR-13 leak check: exactly one new session, both turns share it ----
    sessions_after = _opencode_session_ids(opencode_url)
    new_sessions = sessions_after - sessions_before
    assert new_sessions == {session_first}, (
        f"two-turn reuse must allocate exactly one opencode session; "
        f"saw delta={new_sessions!r}, expected={{{session_first!r}}}"
    )

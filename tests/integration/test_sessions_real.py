"""End-to-end /v1/sessions surface against real opencode + ollama (#10).

Mirrors :mod:`tests.integration.test_chat_real`'s "single integration
test" rationale: the sessions surface (list, per-channel clear, bulk
clear) shares enough setup with /v1/chat that bundling all three
behaviours into one test is cheaper than splitting. Cold-start of
qwen3 dominates the runtime — the two seed chats take ~30s each on a
fresh ollama; the six list/clear calls after that are <1s combined.

Sequence
--------
1. Drive two ``POST /v1/chat`` to create channel A + channel B and
   their opencode sessions.
2. ``GET /v1/sessions`` → both rows present, enriched fields
   populated (title / tokens / cost from opencode).
3. Snapshot opencode's own ``GET /session`` list before any clear.
4. ``POST /v1/sessions/{a}/clear`` → ``cleared=true``.
5. ``GET /v1/sessions`` → only channel B remains.
6. **ADR-13 invariant**: opencode's ``/session`` list is unchanged
   — scufris only drops the local link, never the upstream session.
7. ``POST /v1/sessions/{a}/clear`` again → ``cleared=false``
   (idempotency).
8. ``POST /v1/clear`` → ``count=1`` (B was the last live link).
9. ``GET /v1/sessions`` → empty list.
10. ``POST /v1/clear`` again → ``count=0`` (idempotency).
11. **ADR-13 invariant** again: opencode's ``/session`` list is still
    unchanged after the bulk clear.

Activated with ``-m integration``; conftest fixtures skip the whole
module if opencode or its default model is unreachable, so
``pytest`` (no marker) stays green on CI.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from scufris_server.app import create_app
from scufris_server.config import Settings
from scufris_server.opencode_client import ModelRef

# Two seed chats + 6 fast list/clear calls. test_chat_real budgets 90s
# for three chats; we have two chats + cheap calls so 120s leaves a
# generous cold-start margin without being so loose that a real hang
# would slip through.
WALL_CLOCK_BUDGET_S = 120.0

CHANNEL_A = {"surface": "integration", "surface_id": "sessions_a", "agent": "build"}
CHANNEL_B = {"surface": "integration", "surface_id": "sessions_b", "agent": "build"}

# Default user_id seeded by the lifespan; matches DEFAULT_USER_ID in
# scufris_server.identity. Hardcoded here because no override is set.
DEFAULT_USER_ID = 1


def _opencode_session_ids(opencode_url: str) -> set[str]:
    """Fetch opencode's ``GET /session`` list and return the set of ids.

    Side-channel verification — we hit opencode directly (not through
    scufris) so we can prove ADR-13: scufris's clear endpoints touch
    only our local ``session_links`` table; the opencode session
    itself is never destroyed.

    Auth: matches what ``conftest.opencode_url`` already validated —
    if opencode required ``OPENCODE_SERVER_PASSWORD`` for
    ``/global/health``, the conftest probe would have skipped the
    module already, so we can assume an unauthenticated GET works
    here. If a future opencode build tightens that, this helper will
    need ``httpx.BasicAuth`` like ``OpencodeClient`` uses.
    """
    resp = httpx.get(f"{opencode_url}/session", timeout=5.0)
    resp.raise_for_status()
    body: list[dict[str, Any]] = resp.json()
    return {entry["id"] for entry in body}


@pytest.mark.integration
def test_sessions_real_end_to_end(
    tmp_path: Path,
    opencode_url: str,
    ollama_default_model: ModelRef,
) -> None:
    """One test covering the full session-management round-trip.

    See the module docstring for the 11-step sequence and the rationale
    for keeping it as a single test.
    """
    settings = Settings(state_dir=tmp_path, opencode_url=opencode_url)
    app = create_app(settings)

    started_at = time.monotonic()

    with TestClient(app) as client:
        # ---- 1. Seed two channels via real chats. ----
        resp_a = client.post(
            "/v1/chat",
            json={
                "message": "reply with the single word: pong",
                "channel": CHANNEL_A,
            },
        )
        assert resp_a.status_code == 200, resp_a.text
        session_a = resp_a.json()["oc_session_id"]

        resp_b = client.post(
            "/v1/chat",
            json={"message": "reply with: hi", "channel": CHANNEL_B},
        )
        assert resp_b.status_code == 200, resp_b.text
        session_b = resp_b.json()["oc_session_id"]
        assert session_a != session_b, (
            "two distinct channels must map to distinct opencode sessions"
        )

        # ---- 2. List both rows, assert enrichment is populated. ----
        resp = client.get("/v1/sessions")
        assert resp.status_code == 200, resp.text
        listing = resp.json()
        assert isinstance(listing, list)
        assert len(listing) == 2, f"expected 2 rows, got {listing!r}"
        rows_by_session = {row["oc_session_id"]: row for row in listing}
        assert rows_by_session.keys() == {session_a, session_b}
        for row in listing:
            assert row["title"] is not None, "title should be enriched from opencode"
            tokens = row["tokens"]
            assert tokens is not None, "tokens should be enriched"
            assert tokens["input"] > 0
            assert tokens["output"] > 0
            # ``cost`` may legitimately be 0.0 for ollama (local
            # model, no $); just check it round-tripped as a number.
            assert isinstance(row["cost"], int | float)

        channel_a_id = rows_by_session[session_a]["channel_id"]

        # ---- 3. Snapshot opencode's session list before any clear. ----
        oc_ids_before = _opencode_session_ids(opencode_url)
        assert {session_a, session_b}.issubset(oc_ids_before), (
            f"opencode should know about both seeded sessions; "
            f"got {oc_ids_before & {session_a, session_b}!r}"
        )

        # ---- 4. Clear channel A. ----
        resp = client.post(f"/v1/sessions/{channel_a_id}/clear")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"cleared": True}

        # ---- 5. Listing now shows only channel B. ----
        resp = client.get("/v1/sessions")
        assert resp.status_code == 200, resp.text
        listing = resp.json()
        assert len(listing) == 1
        assert listing[0]["oc_session_id"] == session_b

        # ---- 6. ADR-13: opencode's /session list is unchanged. ----
        oc_ids_after_clear_a = _opencode_session_ids(opencode_url)
        assert {session_a, session_b}.issubset(oc_ids_after_clear_a), (
            "opencode's session list must be unchanged after our clear "
            "(ADR-13 — scufris only drops local links)"
        )

        # ---- 7. Idempotent retry: cleared=False, still 200. ----
        resp = client.post(f"/v1/sessions/{channel_a_id}/clear")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"cleared": False}

        # ---- 8. Bulk clear: only B remained, so count=1. ----
        resp = client.post("/v1/clear", json={"user_id": DEFAULT_USER_ID})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"count": 1}

        # ---- 9. Listing is empty. ----
        resp = client.get("/v1/sessions")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

        # ---- 10. Bulk clear idempotent: count=0. ----
        resp = client.post("/v1/clear", json={"user_id": DEFAULT_USER_ID})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"count": 0}

        # ---- 11. ADR-13 invariant after bulk clear. ----
        oc_ids_after_bulk = _opencode_session_ids(opencode_url)
        assert {session_a, session_b}.issubset(oc_ids_after_bulk), (
            "opencode's session list must be unchanged after bulk clear "
            "(ADR-13 — scufris only drops local links)"
        )

    elapsed = time.monotonic() - started_at
    assert elapsed < WALL_CLOCK_BUDGET_S, (
        f"integration test took {elapsed:.1f}s "
        f"(budget {WALL_CLOCK_BUDGET_S:.0f}s) — investigate"
    )

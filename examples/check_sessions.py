"""Boot scufris-server and exercise the session-management surface.

Purpose
-------
End-to-end smoke for the per-user session endpoints landed in #10:

1. ``GET  /v1/sessions``                       — list with enriched
   ``title`` / ``tokens`` / ``cost`` from opencode.
2. ``POST /v1/sessions/{channel_id}/clear``    — single-channel clear.
3. ``POST /v1/clear``                          — bulk clear.

The script seeds two channels via ``POST /v1/chat`` (so opencode has
real sessions to enrich the listing with), then walks list → clear A
→ list → clear A again (idempotency) → bulk clear → list → bulk
clear again. Asserts the response shapes match the docs and that
both clears are idempotent on retry. Pairs with
``examples/check_chat.py`` — runs the same boot/probe scaffolding,
just exercises a different surface.

How to run
----------
Start opencode first (in another terminal). It needs at least one
connected provider with a default model — locally that's ollama with
``qwen3:latest`` pulled::

    ollama serve                       # if not already running
    ollama pull qwen3:latest           # one-time
    opencode serve --port 4096

Then from the repo root::

    python examples/check_sessions.py

Override defaults via env vars::

    OPENCODE_URL=http://192.0.2.1:4096 \\
      OPENCODE_SERVER_PASSWORD=hunter2 \\
      python examples/check_sessions.py

Expected output
---------------
For each step: a labelled section with status, the response body, and
(where present) the ``X-Request-Id`` header. The script asserts each
contract on the way through; an assertion failure prints the offending
body and the script exits non-zero. Footer: ``OK``.

If ``/v1/chat`` returns 503 with ``error_type=DefaultModelMissing``,
opencode has no connected provider; check ``ollama serve`` and the
provider auth in your opencode config. The script exits non-zero with
the structured error body printed.

Requires
--------
``opencode serve`` reachable at ``OPENCODE_URL`` (default
``http://127.0.0.1:4096``) **and** at least one connected provider
with a default model. With ollama as the default, ``qwen3:latest`` is
the conventional pick. A free TCP port on 127.0.0.1 (auto-picked).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Two short, deterministic-ish prompts. They won't trigger tool calls
# and they cold-start qwen3 the same way ``check_chat.py`` does.
PROMPT_A = "reply with the single word: pong"
PROMPT_B = "reply with: hi"

CHANNEL_A = {"surface": "example", "surface_id": "check_sessions_a", "agent": "build"}
CHANNEL_B = {"surface": "example", "surface_id": "check_sessions_b", "agent": "build"}

# Default user_id seeded by the lifespan (matches ``DEFAULT_USER_ID``
# in ``scufris_server.identity``). We pin the spawned server to this
# user via the ``SCUFRIS_USER_ID`` env var (see ``main`` below) so the
# script is independent of the host's ``config.toml`` and any
# pre-existing ``SCUFRIS_USER_ID`` in the caller's environment. The
# bulk-clear endpoint also takes ``user_id`` in the request body; the
# override wins, so the body value here is documentary — it just
# matches the pin.
DEFAULT_USER_ID = 1

BOOT_TIMEOUT_S = 10.0
CHAT_TIMEOUT_S = 90.0  # cold-start qwen3 can take 15s+
SHUTDOWN_TIMEOUT_S = 5.0
HTTP_TIMEOUT_S = 10.0  # for non-chat (list / clear) calls


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_ready(url: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1.0) as resp:  # noqa: S310 - http://127.0.0.1
                if resp.status == 200:
                    return
        except (URLError, ConnectionError, TimeoutError) as exc:
            last = exc
        time.sleep(0.1)
    raise TimeoutError(f"server not ready within {timeout_s}s (last: {last!r})")


def _get(
    url: str, *, timeout_s: float = HTTP_TIMEOUT_S
) -> tuple[int, dict[str, str], Any]:
    """GET ``url``. Returns ``(status, headers, body)`` even on 4xx/5xx.

    JSON-decodes the body when possible; falls back to a ``{"raw":
    ...}`` dict when the response isn't JSON (defensive — we never
    expect non-JSON from our own endpoints).
    """
    try:
        with urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 - http://127.0.0.1
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, dict(resp.headers), body
    except HTTPError as exc:
        raw = exc.read().decode("utf-8") or "{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"raw": raw}
        return exc.code, dict(exc.headers or {}), body


def _post(
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout_s: float = HTTP_TIMEOUT_S,
) -> tuple[int, dict[str, str], Any]:
    """POST ``url`` with optional JSON ``payload``.

    ``payload=None`` sends an empty body — required for the per-channel
    clear endpoint (FastAPI accepts no body when no pydantic model is
    declared on the route signature).

    Same error handling as :func:`_get`.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else b""
    req = Request(
        url,
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - http://127.0.0.1
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, dict(resp.headers), body
    except HTTPError as exc:
        raw = exc.read().decode("utf-8") or "{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"raw": raw}
        return exc.code, dict(exc.headers or {}), body


def _show(label: str, status: int, headers: dict[str, str], body: Any) -> None:
    """Print one labelled section: header line + status + request id +
    indented JSON body."""
    rid = headers.get("x-request-id") or headers.get("X-Request-Id")
    print(f"=== {label} ===")
    print(f"status:        {status}")
    if rid:
        print(f"x-request-id:  {rid}")
    print("body:")
    print(json.dumps(body, indent=2))
    print()


def main() -> int:  # noqa: PLR0915 - linear narrative reads better as one function
    host = "127.0.0.1"
    port = _pick_free_port()
    base = f"http://{host}:{port}"

    with tempfile.TemporaryDirectory(prefix="scufris-check-sessions-") as td:
        state_dir = Path(td)
        env = {
            **os.environ,
            "SCUFRIS_STATE_DIR": str(state_dir),
            "SCUFRIS_BIND": host,
            "SCUFRIS_PORT": str(port),
            # Pin all three endpoints to user 1 unconditionally. Without
            # this, identity resolution falls through TOML lookups (none
            # match surface=``example``) to the default user — usually 1,
            # but only by accident. An exported ``SCUFRIS_USER_ID`` in
            # the caller's environment would also leak through. Pinning
            # here makes the seed → list → clear → bulk-clear flow
            # deterministic regardless of host config.
            "SCUFRIS_USER_ID": str(DEFAULT_USER_ID),
        }
        cmd = [sys.executable, "-m", "scufris_server"]
        opencode_url = env.get("OPENCODE_URL", "http://127.0.0.1:4096")
        print(f"spawning:    {' '.join(cmd)}")
        print(f"state_dir:   {state_dir}")
        print(f"bind:        {host}:{port}")
        print(f"opencode:    {opencode_url}")
        print()

        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        ok = False
        try:
            _wait_ready(f"{base}/openapi.json", timeout_s=BOOT_TIMEOUT_S)

            # Pre-flight: opencode reachable from the server's POV?
            status, _, body = _get(f"{base}/v1/healthz")
            print("=== GET /v1/healthz (pre-flight) ===")
            print(f"status:  {status}")
            print(f"body:    {json.dumps(body)}")
            print()
            if not body.get("opencode", {}).get("healthy"):
                print(
                    "ERROR: opencode is not reachable from scufris-server. "
                    "Start `opencode serve --port 4096` and rerun."
                )
                return 1

            # 1. Seed channel A — creates an opencode session.
            status, headers, body = _post(
                f"{base}/v1/chat",
                {"message": PROMPT_A, "channel": CHANNEL_A},
                timeout_s=CHAT_TIMEOUT_S,
            )
            _show("POST /v1/chat (seed channel A)", status, headers, body)
            if status != 200:
                print(f"ERROR: seed-A returned {status}")
                return 1
            session_a = body["oc_session_id"]

            # 2. Seed channel B — distinct opencode session.
            status, headers, body = _post(
                f"{base}/v1/chat",
                {"message": PROMPT_B, "channel": CHANNEL_B},
                timeout_s=CHAT_TIMEOUT_S,
            )
            _show("POST /v1/chat (seed channel B)", status, headers, body)
            if status != 200:
                print(f"ERROR: seed-B returned {status}")
                return 1
            session_b = body["oc_session_id"]
            assert session_a != session_b, (
                "channels A and B must get distinct opencode sessions"
            )

            # 3. List: both rows present, enriched fields populated.
            status, headers, body = _get(f"{base}/v1/sessions")
            _show("GET /v1/sessions (after seeding)", status, headers, body)
            assert status == 200, f"list returned {status}"
            assert isinstance(body, list), f"expected list, got {type(body).__name__}"
            assert len(body) == 2, f"expected 2 rows, got {len(body)}"
            ids = {row["oc_session_id"] for row in body}
            assert ids == {session_a, session_b}, (
                f"unexpected session ids: {ids!r} vs {session_a!r}/{session_b!r}"
            )
            for row in body:
                assert row["title"] is not None, (
                    "title should be enriched from opencode"
                )
                tokens = row["tokens"]
                assert tokens is not None, "tokens should be enriched"
                assert tokens["input"] > 0, f"input tokens not populated: {tokens}"
                assert tokens["output"] > 0, f"output tokens not populated: {tokens}"

            # Pick channel A's id by matching oc_session_id (we don't
            # know the channel_id ahead of time — chat doesn't return it).
            channel_a_id = next(
                row["channel_id"] for row in body if row["oc_session_id"] == session_a
            )

            # 4. Per-channel clear: cleared=True on first call.
            status, headers, body = _post(f"{base}/v1/sessions/{channel_a_id}/clear")
            _show(
                f"POST /v1/sessions/{channel_a_id}/clear (first call)",
                status,
                headers,
                body,
            )
            assert status == 200
            assert body == {"cleared": True}, f"unexpected body: {body!r}"

            # 5. List: only channel B remains.
            status, headers, body = _get(f"{base}/v1/sessions")
            _show("GET /v1/sessions (after clearing A)", status, headers, body)
            assert status == 200
            assert len(body) == 1, f"expected 1 row, got {len(body)}"
            assert body[0]["oc_session_id"] == session_b

            # 6. Idempotent retry: cleared=False.
            status, headers, body = _post(f"{base}/v1/sessions/{channel_a_id}/clear")
            _show(
                f"POST /v1/sessions/{channel_a_id}/clear (idempotent retry)",
                status,
                headers,
                body,
            )
            assert status == 200
            assert body == {"cleared": False}, f"unexpected body on retry: {body!r}"

            # 7. Bulk clear (default user, id=1).
            status, headers, body = _post(
                f"{base}/v1/clear", {"user_id": DEFAULT_USER_ID}
            )
            _show("POST /v1/clear (bulk)", status, headers, body)
            assert status == 200
            assert body == {"count": 1}, f"expected count=1, got {body!r}"

            # 8. List: empty.
            status, headers, body = _get(f"{base}/v1/sessions")
            _show("GET /v1/sessions (after bulk clear)", status, headers, body)
            assert status == 200
            assert body == [], f"expected empty list, got {body!r}"

            # 9. Bulk clear again — count=0.
            status, headers, body = _post(
                f"{base}/v1/clear", {"user_id": DEFAULT_USER_ID}
            )
            _show("POST /v1/clear (idempotent retry)", status, headers, body)
            assert status == 200
            assert body == {"count": 0}, f"expected count=0 on retry, got {body!r}"

            ok = True
        except (AssertionError, TimeoutError, URLError) as exc:
            print(f"ERROR: {exc!r}")
        finally:
            print("shutting down")
            proc.terminate()
            try:
                out, _ = proc.communicate(timeout=SHUTDOWN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
            if not ok and out:
                print()
                print("subprocess output:")
                print(out, end="" if out.endswith("\n") else "\n")

    print()
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

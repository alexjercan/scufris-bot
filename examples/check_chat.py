"""Boot scufris-server and exercise POST /v1/chat against live opencode.

Purpose
-------
This is the end-to-end smoke for the v0 happy path: start
``scufris-server`` against a real ``opencode serve``, ask ollama
(via opencode) a tiny question, and verify channel-scoped session
plumbing works.

Three POSTs in sequence:

1. ``POST /v1/chat`` with channel ``(example, check_chat, build)`` —
   creates a new opencode session (slow on a cold model).
2. ``POST /v1/chat`` to the *same* channel — reuses the session
   (asserts ``oc_session_id`` matches the first response).
3. ``POST /v1/chat`` to ``(example, check_chat_other, build)`` —
   confirms a distinct channel triple gets its own session
   (asserts ``oc_session_id`` differs).

How to run
----------
Start opencode first (in another terminal). It needs at least one
connected provider with a default model — locally that's ollama with
``qwen3:latest`` pulled::

    ollama serve                       # if not already running
    ollama pull qwen3:latest           # one-time
    opencode serve --port 4096

Then from the repo root::

    python examples/check_chat.py

Override defaults via env vars::

    OPENCODE_URL=http://192.0.2.1:4096 \\
      OPENCODE_SERVER_PASSWORD=hunter2 \\
      python examples/check_chat.py

Expected output
---------------
For each POST: status 200, the assistant reply text, the
``oc_session_id`` and ``oc_message_id``, the ``tokens`` shape, and
the ``X-Request-Id`` from the response. The script asserts the
first two share an ``oc_session_id`` and the third has a different
one. Footer: ``OK``.

If ``/v1/chat`` returns 503 with ``error_type=DefaultModelMissing``,
opencode has no connected provider; check ``ollama serve`` and the
provider auth in your opencode config. The script exits non-zero
with the structured error body printed.

Requires
--------
``opencode serve`` reachable at ``OPENCODE_URL`` (default
``http://127.0.0.1:4096``) **and** at least one connected provider
with a default model. With ollama as the default, ``qwen3:latest``
is the conventional pick. A free TCP port on 127.0.0.1 (auto-picked).
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

# Demo prompts: short, deterministic-ish, won't trigger tool calls.
PROMPT_FIRST = "reply with the single word: pong"
PROMPT_SECOND = "reply with the single word: ack"
PROMPT_OTHER = "reply with: hi"

CHANNEL_A = {"surface": "example", "surface_id": "check_chat", "agent": "build"}
CHANNEL_B = {"surface": "example", "surface_id": "check_chat_other", "agent": "build"}

BOOT_TIMEOUT_S = 10.0
CHAT_TIMEOUT_S = 90.0  # cold-start qwen3 can take 15s+; budget for three round-trips
SHUTDOWN_TIMEOUT_S = 5.0


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


def _get_json(url: str) -> tuple[int, dict[str, str], dict[str, Any]]:
    with urlopen(url, timeout=5.0) as resp:  # noqa: S310 - http://127.0.0.1
        body = json.loads(resp.read().decode("utf-8"))
        return resp.status, dict(resp.headers), body


def _post_chat(
    url: str, message: str, channel: dict[str, str]
) -> tuple[int, dict[str, str], dict[str, Any]]:
    """POST /v1/chat. Returns (status, headers, body) even on 4xx/5xx."""
    payload = json.dumps({"message": message, "channel": channel}).encode("utf-8")
    req = Request(
        url,
        data=payload,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=CHAT_TIMEOUT_S) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, dict(resp.headers), body
    except HTTPError as exc:
        # urlopen raises on 4xx/5xx; we still want the body for diagnostics.
        raw = exc.read().decode("utf-8") or "{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"raw": raw}
        return exc.code, dict(exc.headers or {}), body


def _show_chat(
    label: str, status: int, headers: dict[str, str], body: dict[str, Any]
) -> None:
    rid = headers.get("x-request-id") or headers.get("X-Request-Id")
    print(f"=== {label} ===")
    print(f"status:        {status}")
    print(f"x-request-id:  {rid}")
    print("body:")
    print(json.dumps(body, indent=2))
    print()


def main() -> int:
    host = "127.0.0.1"
    port = _pick_free_port()
    base = f"http://{host}:{port}"

    with tempfile.TemporaryDirectory(prefix="scufris-check-chat-") as td:
        state_dir = Path(td)
        env = {
            **os.environ,
            "SCUFRIS_STATE_DIR": str(state_dir),
            "SCUFRIS_BIND": host,
            "SCUFRIS_PORT": str(port),
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

            # Pre-flight: does opencode look reachable from the server's POV?
            # /v1/healthz aggregates the lifespan probe + a fresh /global/health.
            status, _, body = _get_json(f"{base}/v1/healthz")
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

            # 1. First POST: new channel, expect a fresh opencode session.
            status, headers, body_a1 = _post_chat(
                f"{base}/v1/chat", PROMPT_FIRST, CHANNEL_A
            )
            _show_chat("POST /v1/chat (channel A, new)", status, headers, body_a1)
            if status != 200:
                print(f"ERROR: expected 200, got {status}")
                if isinstance(body_a1, dict) and "detail" in body_a1:
                    detail = body_a1["detail"]
                    if isinstance(detail, dict) and detail.get("error_type"):
                        print(
                            f"hint:  error_type={detail['error_type']!r} — "
                            "see the docstring for diagnoses."
                        )
                return 1
            session_a = body_a1["oc_session_id"]

            # 2. Same channel: must reuse session.
            status, headers, body_a2 = _post_chat(
                f"{base}/v1/chat", PROMPT_SECOND, CHANNEL_A
            )
            _show_chat("POST /v1/chat (channel A, reuse)", status, headers, body_a2)
            assert status == 200, f"reuse call returned {status}"
            assert body_a2["oc_session_id"] == session_a, (
                f"same channel must reuse session: "
                f"{body_a2['oc_session_id']!r} != {session_a!r}"
            )

            # 3. Different channel: must get a distinct session.
            status, headers, body_b = _post_chat(
                f"{base}/v1/chat", PROMPT_OTHER, CHANNEL_B
            )
            _show_chat("POST /v1/chat (channel B, distinct)", status, headers, body_b)
            assert status == 200, f"channel B call returned {status}"
            assert body_b["oc_session_id"] != session_a, (
                "distinct channel must get its own session"
            )

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

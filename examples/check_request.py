"""Boot scufris-server, then exercise GET /v1/healthz and GET /v1/version.

Purpose
-------
Launches scufris-server in a subprocess (temp state dir, auto-picked
free port), waits for it to be ready, then makes three demo requests
and pretty-prints what came back:

1. ``GET /v1/healthz`` with a custom ``X-Request-Id`` header — proves
   the middleware echoes the caller-supplied ID instead of minting a
   new one.
2. ``GET /v1/version`` — proves the middleware mints a fresh ULID.
3. ``GET /v1/version`` again — proves the response is identical
   (cache hit; no extra opencode probe).

How to run
----------
From the repo root::

    python examples/check_request.py

If opencode is reachable, ``/v1/healthz`` reports ``ok: true`` with a
version, and ``opencode_version`` on ``/v1/version`` is non-null. If
not, ``ok: false`` with an error string, and ``opencode_version`` is
``null``. Either way the script passes — these endpoints are
designed to stay green even when opencode is down.

Requires
--------
A free TCP port on 127.0.0.1 (picked automatically). opencode
optional.
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
from urllib.error import URLError
from urllib.request import Request, urlopen

# A real, valid ULID we hand to /v1/healthz to demonstrate the
# echo-incoming-id branch of the middleware. Any 26-char Crockford
# base32 string with the right structure works.
DEMO_REQUEST_ID = "01J0AB7E0KCSV0K5AYY1X14J9G"

BOOT_TIMEOUT_S = 10.0
SHUTDOWN_TIMEOUT_S = 5.0


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_ready(url: str, timeout_s: float) -> None:
    """Poll ``url`` until it returns 200 or we exhaust the budget."""
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


def _request(
    url: str, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], dict[str, Any]]:
    req = Request(url, headers=headers or {})
    with urlopen(req, timeout=5.0) as resp:  # noqa: S310 - http://127.0.0.1
        body = json.loads(resp.read().decode("utf-8"))
        return resp.status, dict(resp.headers), body


def _show(
    label: str, status: int, headers: dict[str, str], body: dict[str, Any]
) -> None:
    print(f"=== {label} ===")
    print(f"status:        {status}")
    print(
        f"x-request-id:  {headers.get('x-request-id') or headers.get('X-Request-Id')}"
    )
    print("body:")
    print(json.dumps(body, indent=2))
    print()


def main() -> int:
    host = "127.0.0.1"
    port = _pick_free_port()
    base = f"http://{host}:{port}"

    with tempfile.TemporaryDirectory(prefix="scufris-check-request-") as td:
        state_dir = Path(td)
        env = {
            **os.environ,
            "SCUFRIS_STATE_DIR": str(state_dir),
            "SCUFRIS_BIND": host,
            "SCUFRIS_PORT": str(port),
        }
        cmd = [sys.executable, "-m", "scufris_server"]
        print(f"spawning:  {' '.join(cmd)}")
        print(f"state_dir: {state_dir}")
        print(f"bind:      {host}:{port}")
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

            # 1. /v1/healthz with a caller-supplied request id.
            status, headers, body = _request(
                f"{base}/v1/healthz",
                headers={"X-Request-Id": DEMO_REQUEST_ID},
            )
            _show(
                f"GET /v1/healthz (X-Request-Id: {DEMO_REQUEST_ID})",
                status,
                headers,
                body,
            )
            echoed = headers.get("x-request-id") or headers.get("X-Request-Id")
            assert echoed == DEMO_REQUEST_ID, (
                f"middleware did not echo our id: got {echoed!r}"
            )

            # 2. /v1/version — middleware should mint a fresh ULID.
            status, headers, body = _request(f"{base}/v1/version")
            _show("GET /v1/version (first call)", status, headers, body)
            first = body
            first_rid = headers.get("x-request-id") or headers.get("X-Request-Id")
            assert first_rid != DEMO_REQUEST_ID, "expected a fresh ULID"

            # 3. /v1/version again — cache hit; body must be identical.
            status, headers, body = _request(f"{base}/v1/version")
            _show("GET /v1/version (second call, cache hit)", status, headers, body)
            assert body == first, "version response should be identical (cache hit)"
            second_rid = headers.get("x-request-id") or headers.get("X-Request-Id")
            assert second_rid != first_rid, "each request gets its own id"

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

"""Boot scufris-server in a subprocess and verify it serves OpenAPI.

Purpose
-------
Launch ``python -m scufris_server`` with a temp ``SCUFRIS_STATE_DIR``
and an auto-picked ``SCUFRIS_PORT``, wait up to ~10s for it to come
up, then fetch ``/openapi.json`` and inspect the response shape.
Tears the subprocess down cleanly on every exit path.

How to run
----------
From the repo root::

    python examples/check_boot.py

Expected output
---------------
- "spawning: ..."
- "boot: ready after Xms"
- "openapi: title='scufris-server', version='0.1.0', paths=[...]"
- "shutting down"
- "OK"

If boot fails, the captured subprocess output is dumped before exit.

opencode does **not** need to be running. If it isn't, the lifespan
boots in degraded mode (a single WARNING about unreachable opencode)
and this script still passes — the goal is to prove the app factory
and lifespan come up cleanly.

Requires
--------
- A free TCP port on 127.0.0.1 (picked automatically).
- ``scufris_server`` importable in the current environment.
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
from urllib.request import urlopen

BOOT_TIMEOUT_S = 10.0
SHUTDOWN_TIMEOUT_S = 5.0


def _pick_free_port() -> int:
    """Ask the kernel for a currently-unused TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _poll_openapi(url: str, timeout_s: float) -> dict[str, Any]:
    """Poll ``url`` until it returns 200, retrying every 100ms."""
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1.0) as resp:  # noqa: S310 - http://127.0.0.1
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except (URLError, ConnectionError, TimeoutError) as exc:
            last_err = exc
        time.sleep(0.1)
    raise TimeoutError(
        f"openapi did not become available at {url} within {timeout_s}s "
        f"(last error: {last_err!r})"
    )


def main() -> int:
    host = "127.0.0.1"
    port = _pick_free_port()
    openapi_url = f"http://{host}:{port}/openapi.json"

    with tempfile.TemporaryDirectory(prefix="scufris-check-boot-") as td:
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
        print(f"url:       {openapi_url}")
        print()

        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        t0 = time.monotonic()
        ok = False
        try:
            schema = _poll_openapi(openapi_url, timeout_s=BOOT_TIMEOUT_S)
            elapsed_ms = (time.monotonic() - t0) * 1000
            print(f"boot: ready after {elapsed_ms:.0f}ms")

            info = schema.get("info", {})
            paths = sorted(schema.get("paths", {}).keys())
            print(
                f"openapi: title={info.get('title')!r}, "
                f"version={info.get('version')!r}, "
                f"paths={paths}"
            )
            ok = True
        except TimeoutError as exc:
            print(f"ERROR: {exc}")
        finally:
            print()
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

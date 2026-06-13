"""Probe the local opencode daemon's health via :class:`OpencodeClient`.

Purpose
-------
Construct an :class:`OpencodeClient` from the current env (or
defaults) and call :meth:`OpencodeClient.health`. Prints the version
and healthy flag on success; prints a diagnosable error on failure.

How to run
----------
Start opencode first (in another terminal)::

    opencode serve --port 4096

Then from the repo root::

    python examples/check_opencode_health.py

Override the URL or auth with env vars::

    OPENCODE_URL=http://192.0.2.1:4096 \\
      OPENCODE_SERVER_PASSWORD=hunter2 \\
      python examples/check_opencode_health.py

Expected output
---------------
- "url:      http://127.0.0.1:4096"
- "version:  1.x.y"
- "healthy:  True"
- "OK" footer

Requires
--------
``opencode serve`` reachable at ``OPENCODE_URL`` (default
``http://127.0.0.1:4096``). ``OPENCODE_SERVER_PASSWORD`` honored
when set.
"""

from __future__ import annotations

import asyncio
import sys

from scufris_server.config import Settings
from scufris_server.opencode_client import OpencodeClient, OpencodeUnavailable


async def run() -> int:
    s = Settings()
    pw = "<set>" if s.opencode_password else "<unset>"
    print(f"url:      {s.opencode_url}")
    print(f"password: {pw}")
    print()

    async with OpencodeClient(s.opencode_url, s.opencode_password) as client:
        try:
            health = await client.health()
        except OpencodeUnavailable as exc:
            print(f"ERROR: {exc}")
            print()
            print("Is opencode running? Try: opencode serve --port 4096")
            return 1

    print(f"version:  {health.version}")
    print(f"healthy:  {health.healthy}")
    print()
    print("OK")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())

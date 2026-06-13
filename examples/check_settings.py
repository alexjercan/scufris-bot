"""Print the effective scufris-server settings from env + defaults.

Purpose
-------
Construct a :class:`Settings` instance from the current environment
and print every field, plus the raw ``SCUFRIS_*`` / ``OPENCODE_*`` /
``XDG_STATE_HOME`` env vars that fed it. Useful for verifying env
overrides land in the right place. Also exercises the unsafe-config
warning so you can see when it fires.

How to run
----------
From the repo root::

    python examples/check_settings.py

    # Or with overrides:
    SCUFRIS_PORT=9000 OPENCODE_URL=http://10.0.0.1:4096 \\
      python examples/check_settings.py

Expected output
---------------
- One line per :class:`Settings` field with its resolved value.
- The env-var inventory (which are set, which are not).
- The unsafe-config warning if password is unset *and* opencode URL
  is non-loopback; otherwise "no warnings".

Requires
--------
Just the ``scufris_server`` package. No opencode, no network.
"""

from __future__ import annotations

import os
import warnings

from scufris_server.config import Settings, warn_unsafe_settings

_ENV_VARS = (
    "SCUFRIS_BIND",
    "SCUFRIS_PORT",
    "SCUFRIS_STATE_DIR",
    "OPENCODE_URL",
    "OPENCODE_SERVER_PASSWORD",
    "XDG_STATE_HOME",
)


def main() -> int:
    s = Settings()

    print("effective Settings:")
    print(f"  bind              = {s.bind!r}")
    print(f"  port              = {s.port!r}")
    print(f"  opencode_url      = {s.opencode_url!r}")
    pw = "<set>" if s.opencode_password else "<unset>"
    print(f"  opencode_password = {pw}")
    print(f"  state_dir         = {s.state_dir!r}")

    print()
    print("relevant env vars:")
    for var in _ENV_VARS:
        value = os.environ.get(var)
        if value is None:
            print(f"  {var:<26} (unset)")
        elif "PASSWORD" in var:
            print(f"  {var:<26} <set, {len(value)} chars>")
        else:
            print(f"  {var:<26} = {value!r}")

    print()
    print("safety check:")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warn_unsafe_settings(s)
    if caught:
        for w in caught:
            print(f"  WARN: {w.message}")
    else:
        print("  no warnings (config is safe for this env)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

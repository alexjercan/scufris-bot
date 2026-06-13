"""Integration-test fixtures (step 9 of #9).

These fixtures probe a real running ``opencode serve`` (default
``http://127.0.0.1:4096``) and the configured default model. If
either is unavailable the whole module is skipped, so the offline
unit-test suite keeps passing on CI without these services.

Activation
----------
Module-scoped fixtures so the skip kicks once per file rather than
once per test.

The integration tests hit live opencode, which in turn hits live
ollama (or whatever provider opencode declares as default). We do
not mock anything in this directory.

Environment
-----------
``OPENCODE_URL`` overrides the opencode base URL (default
``http://127.0.0.1:4096``).
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from scufris_server.opencode_client import ModelRef, OpencodeClient

DEFAULT_OPENCODE_URL = "http://127.0.0.1:4096"
PROBE_TIMEOUT_S = 5.0


@pytest.fixture(scope="module")
def opencode_url() -> str:
    """Return a reachable opencode URL or skip the module.

    Probes ``GET /global/health`` once with a short timeout. We treat
    *any* failure (network error, non-200, non-JSON, ``healthy=False``)
    as "opencode unavailable" and skip — running integration tests
    against a half-broken opencode would produce noisy and misleading
    failures.
    """
    url = os.getenv("OPENCODE_URL", DEFAULT_OPENCODE_URL)
    try:
        resp = httpx.get(f"{url}/global/health", timeout=PROBE_TIMEOUT_S)
    except httpx.HTTPError as exc:
        pytest.skip(f"opencode unreachable at {url}: {exc}")

    if resp.status_code != 200:
        pytest.skip(
            f"opencode at {url} returned {resp.status_code} (expected 200)"
        )

    try:
        body: dict[str, Any] = resp.json()
    except ValueError:
        pytest.skip(f"opencode at {url} did not return JSON")

    if not body.get("healthy"):
        pytest.skip(f"opencode at {url} reports healthy=False")

    return url


@pytest.fixture(scope="module")
def ollama_default_model(opencode_url: str) -> ModelRef:
    """Return the default model opencode would pick, or skip.

    Runs the same algorithm as :meth:`OpencodeClient.get_default_model`:
    walks ``connected`` and returns the first provider that has a
    matching ``default[provider]`` entry. Skips if no such pair exists,
    because the chat handler itself would 503 in that situation.

    The fixture name says "ollama" because that's our local default,
    but the algorithm is provider-agnostic — if you've configured
    opencode with a different default the test will use it.
    """

    async def _probe() -> ModelRef | None:
        async with OpencodeClient(opencode_url) as client:
            return await client.get_default_model()

    import asyncio

    model = asyncio.run(_probe())
    if model is None:
        pytest.skip(
            f"opencode at {opencode_url} has no connected provider "
            "with a default model — integration tests cannot run"
        )
    return model

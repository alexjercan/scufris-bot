"""Imports each placeholder module created by step 10 of task #9 and
asserts the minimal contract dependent tasks can rely on:

- The module imports cleanly (no syntax errors, no missing deps).
- It carries a docstring (step 10 acceptance: "Each carries a docstring
  naming the owning task ID").
- For HTTP route placeholders, ``router`` is an empty ``APIRouter`` so
  it's safe to mount / merge before the owning task lands real routes.
- For non-HTTP placeholders, no symbols are exported beyond the
  docstring + the implicit ``__name__``/``__doc__`` machinery.

If a future task forgets to remove the empty-router assertion when it
adds real routes, that's a deliberate trip-wire telling them to also
update :data:`scufris_server.routes.ROUTERS`.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import APIRouter

# (module, owning_task_id, expected_router_prefix or None)
HTTP_PLACEHOLDERS: list[tuple[str, str, str]] = [
    ("scufris_server.routes.identity", "20260613-091046", "/v1/identity"),
    ("scufris_server.routes.sessions", "20260613-091044", "/v1/sessions"),
    ("scufris_server.routes.stats", "20260613-091047", "/v1/stats"),
    ("scufris_server.routes.permissions", "20260613-093108", "/v1/permissions"),
]

NON_HTTP_PLACEHOLDERS: list[tuple[str, str]] = [
    ("scufris_server.events", "20260613-091045"),
    ("scufris_server.internal", "20260613-093857"),
    ("scufris_server.identity", "20260613-091046"),
]


@pytest.mark.parametrize(
    ("module_name", "task_id", "expected_prefix"),
    HTTP_PLACEHOLDERS,
)
def test_http_placeholder_module(
    module_name: str, task_id: str, expected_prefix: str
) -> None:
    mod = importlib.import_module(module_name)

    # Docstring exists and references the owning task.
    assert mod.__doc__, f"{module_name} missing docstring"
    assert task_id in mod.__doc__, (
        f"{module_name} docstring must reference owning task {task_id}; "
        f"got {mod.__doc__!r}"
    )

    # Module exposes a router.
    router = getattr(mod, "router", None)
    assert isinstance(router, APIRouter), (
        f"{module_name}.router must be an APIRouter; got {type(router)!r}"
    )
    assert router.prefix == expected_prefix
    # Empty: no routes yet. The owning task will add them.
    assert router.routes == [], (
        f"{module_name}.router has unexpected routes — "
        "step 10 placeholders must stay empty"
    )


@pytest.mark.parametrize(
    ("module_name", "task_id"),
    NON_HTTP_PLACEHOLDERS,
)
def test_non_http_placeholder_module(module_name: str, task_id: str) -> None:
    mod = importlib.import_module(module_name)

    assert mod.__doc__, f"{module_name} missing docstring"
    assert task_id in mod.__doc__, (
        f"{module_name} docstring must reference owning task {task_id}; "
        f"got {mod.__doc__!r}"
    )

    # No public symbols expected. The dunder prefix filter mirrors
    # :pep:`8`'s "public API" convention.
    public = [name for name in dir(mod) if not name.startswith("_")]
    # ``annotations`` from ``from __future__ import annotations`` is allowed.
    public = [name for name in public if name != "annotations"]
    assert public == [], (
        f"{module_name} unexpectedly exports {public}; placeholders "
        "should expose nothing until the owning task lands"
    )


def test_routes_init_imports_placeholders() -> None:
    """``scufris_server.routes`` imports placeholders so import-time
    breakage shows up immediately rather than only when the owning
    task lands. Mounting them is the owning task's job."""
    import scufris_server.routes as routes_pkg

    # Placeholders should be reachable via the package namespace
    # (we did ``from scufris_server.routes import identity, ...``).
    for attr in ("identity", "sessions", "stats", "permissions"):
        assert hasattr(routes_pkg, attr), (
            f"scufris_server.routes.{attr} not imported by routes/__init__.py"
        )

    # ROUTERS still only has health + chat — placeholders are *not*
    # mounted by step 10.
    from scufris_server.routes import ROUTERS

    assert len(ROUTERS) == 2, (
        f"step 10 must not auto-mount placeholders; ROUTERS has {len(ROUTERS)} entries"
    )

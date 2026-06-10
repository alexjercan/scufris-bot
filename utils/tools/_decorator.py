"""Minimal ``@tool`` decorator shim.

LangChain's ``@tool`` decorator was previously used to expose tools
to the agent runtime. The OpenCode-runtime swap (task
``20260610-101413``) means tools are no longer invoked through the
agent loop, but the test suite still uses the LangChain-style
``.invoke({...})`` interface.

This shim preserves that surface area without pulling in
``langchain.tools``. The decorator returns a small wrapper exposing:

- ``.invoke(kwargs: Mapping[str, Any]) -> str`` — calls the function
  with the provided keyword args (mirrors LangChain's ``StructuredTool``).
- ``.func`` — the raw function, for tests that want to bypass the
  wrapper (e.g. to test invalid input that the schema layer would
  otherwise reject).
- ``.name`` — the tool name (function name by default; overridable
  via ``@tool("custom_name")``).

Two call shapes are supported, matching LangChain's behaviour:

- ``@tool`` (bare) — ``name`` defaults to the function name.
- ``@tool("custom_name")`` — explicit name override.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional


class Tool:
    """Wraps a function to provide a LangChain-style ``.invoke`` surface."""

    def __init__(self, func: Callable[..., str], name: Optional[str] = None) -> None:
        self.func = func
        self.name = name or func.__name__
        self.__doc__ = func.__doc__

    def invoke(self, kwargs: Optional[Mapping[str, Any]] = None) -> str:
        """Call the underlying function with ``kwargs`` as keyword args.

        ``None`` and ``{}`` are both acceptable for "no arguments"; the
        function's own defaults take over.
        """
        return self.func(**dict(kwargs or {}))

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        """Direct call passthrough — kept so the wrapper is drop-in for
        plain function callsites that don't go through ``.invoke``."""
        return self.func(*args, **kwargs)


def tool(name_or_func: Any = None) -> Any:
    """``@tool`` / ``@tool("name")`` decorator factory.

    Detects which call shape was used by checking whether the
    argument is callable: bare ``@tool`` hands the function in
    directly; ``@tool("name")`` hands in the name string and returns
    a decorator.
    """
    if callable(name_or_func):
        # @tool (no parens) — name_or_func is the function.
        return Tool(name_or_func)

    # @tool("name") — return a decorator that captures the name.
    name = name_or_func

    def decorator(func: Callable[..., str]) -> Tool:
        return Tool(func, name=name)

    return decorator

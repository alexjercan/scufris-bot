"""Console-script entry point: ``python -m scufris_server`` / ``scufris-server``.

Reads ``bind`` and ``port`` from :class:`Settings` (i.e. ``SCUFRIS_BIND``
and ``SCUFRIS_PORT`` env vars), installs JSON logging on the
``scufris_server`` logger, then hands off to uvicorn.

We pass ``log_config=None`` so uvicorn does not configure the root
logger — our :func:`setup_logging` owns scufris's output, and uvicorn's
own access/error loggers stay on their defaults (stderr, plain text)
until #28 unifies them.
"""

from __future__ import annotations

import uvicorn

from scufris_server.config import get_settings
from scufris_server.logging import setup_logging


def main() -> None:
    """Run uvicorn against the FastAPI factory with env-driven bind/port."""
    setup_logging()
    settings = get_settings()
    uvicorn.run(
        "scufris_server.app:create_app",
        factory=True,
        host=settings.bind,
        port=settings.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()

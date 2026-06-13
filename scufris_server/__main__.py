"""Console-script entry point: ``python -m scufris_server`` / ``scufris-server``.

Reads ``bind`` and ``port`` from :class:`Settings` (i.e. ``SCUFRIS_BIND``
and ``SCUFRIS_PORT`` env vars), then hands off to uvicorn. Uvicorn calls
our app factory once on startup; if the user wants per-worker reloads
they can run ``uvicorn scufris_server.app:create_app --factory`` directly
with whatever flags they like.
"""

from __future__ import annotations

import uvicorn

from scufris_server.config import get_settings


def main() -> None:
    """Run uvicorn against the FastAPI factory with env-driven bind/port."""
    settings = get_settings()
    uvicorn.run(
        "scufris_server.app:create_app",
        factory=True,
        host=settings.bind,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()

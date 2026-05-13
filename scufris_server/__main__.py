"""``python -m scufris_server`` / ``scufris-server`` entrypoint."""

from __future__ import annotations

import logging
import os

import uvicorn

from utils.logging import setup_logging


def main() -> None:
    """Launch the uvicorn server bound to ``SCUFRIS_BIND``:``SCUFRIS_PORT``."""
    log_level = os.environ.get("SCUFRIS_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level, logging.INFO)
    setup_logging(level=level)

    bind = os.environ.get("SCUFRIS_BIND", "127.0.0.1")
    port = int(os.environ.get("SCUFRIS_PORT", "8765"))

    uvicorn.run(
        "scufris_server.app:create_app",
        factory=True,
        host=bind,
        port=port,
        workers=1,
        log_level=log_level.lower(),
    )


if __name__ == "__main__":
    main()

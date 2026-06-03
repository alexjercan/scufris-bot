"""``python -m scufris_server`` / ``scufris-server`` entrypoint."""

from __future__ import annotations

import logging

import uvicorn

from utils import load_config
from utils.logging import setup_logging


def main() -> None:
    """Launch uvicorn bound to ``server.bind:server.port`` from the
    unified config.

    The config file is loaded eagerly here purely to read out the
    listen address and log level — the same call inside
    :func:`scufris_server.bootstrap.build_runtime` (invoked by the
    FastAPI lifespan) is the one whose result actually drives the agent.
    """
    config = load_config(require_telegram=False)
    log_level = (config.server.log_level or "INFO").upper()
    level = getattr(logging, log_level, logging.INFO)
    setup_logging(level=level)

    uvicorn.run(
        "scufris_server.app:create_app",
        factory=True,
        host=config.server.bind,
        port=config.server.port,
        workers=1,
        log_level=log_level.lower(),
    )


if __name__ == "__main__":
    main()

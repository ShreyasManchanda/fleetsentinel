"""Process entrypoint (excluded from the unit-coverage gate)."""
from __future__ import annotations

import logging
import sys

import structlog


def configure_logging() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[structlog.processors.TimeStamper(fmt="iso"), structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def main() -> None:
    configure_logging()
    log = structlog.get_logger("fs_lake_sink.main")
    log.info("lake_sink_start")


if __name__ == "__main__":
    main()

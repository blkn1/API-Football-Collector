from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import structlog


def setup_logging(
    *,
    level: str | None = None,
    log_file: str | None = None,
) -> None:
    """
    Structured logging (Phase 2)
    - JSON to console
    - JSON lines to file (optional)
    """
    lvl = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    file_path = log_file or os.getenv("LOG_FILE", "logs/collector.jsonl")

    root = logging.getLogger()
    root.setLevel(lvl)
    root.handlers.clear()

    formatter = logging.Formatter("%(message)s")

    console = logging.StreamHandler()
    console.setLevel(lvl)
    console.setFormatter(formatter)
    root.addHandler(console)

    if file_path:
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(file_path)
        fh.setLevel(lvl)
        fh.setFormatter(formatter)
        root.addHandler(fh)

    # Silence noisy HTTP client logs unless explicitly enabled.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, lvl, logging.INFO)),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(**kwargs: Any):
    return structlog.get_logger().bind(**kwargs)



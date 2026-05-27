"""Structured logging via structlog.

JSON output in production; colored pretty-print in dev.
Every log entry must include transaction_id, user_id (hashed), partition_id
when available — those fields are injected via contextvars by the consumer.
"""

from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(log_level: str = "INFO") -> None:
    """Configure structlog for JSON output."""
    log_level_int = getattr(logging, log_level.upper(), logging.INFO)

    # Configure stdlib logging so third-party libs flow through structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level_int,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


# Module-level logger — import this wherever you need to log
logger: structlog.stdlib.BoundLogger = structlog.get_logger()

import logging
from pathlib import Path

import httpx
import structlog


def post_discord(webhook_url: str, message: str) -> None:
    try:
        httpx.post(webhook_url, json={"content": message[:1900]}, timeout=10.0)
    except httpx.HTTPError:
        pass  # never let alerting failures crash the run


def configure_logging(log_dir: Path, webhook_url: str | None) -> structlog.BoundLogger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_dir / "rabbit_hole.log"),
        level=logging.INFO,
        format="%(message)s",
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
    return structlog.get_logger()

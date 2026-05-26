import logging
from pathlib import Path

import httpx
import structlog


def post_discord(webhook_url: str, message: str) -> None:
    if not webhook_url:
        return
    try:
        httpx.post(webhook_url, json={"content": message[:1900]}, timeout=10.0)
    except Exception:  # noqa: BLE001 - alerting must never crash the run
        pass


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

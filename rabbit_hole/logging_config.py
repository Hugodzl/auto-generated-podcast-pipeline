import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
import structlog
import structlog.stdlib


def post_discord(webhook_url: str, message: str) -> None:
    if not webhook_url:
        return
    try:
        httpx.post(webhook_url, json={"content": message[:1900]}, timeout=10.0)
    except Exception:  # noqa: BLE001 - alerting must never crash the run
        pass


def configure_logging(log_dir: Path, webhook_url: str | None) -> structlog.BoundLogger:
    log_dir.mkdir(parents=True, exist_ok=True)
    # Route everything to a UTF-8 file handler on the root logger. structlog's
    # default factory writes to stdout, which is discarded under the headless
    # pythonw.exe scheduled run; an explicit FileHandler (re)installed on each
    # call keeps the JSON event stream in rabbit_hole.log. UTF-8 + ensure_ascii
    # =False so non-ASCII paper titles are written readably instead of crashing
    # on a cp1252 host.
    # Rotation is a safety net: with third-party noise silenced a weekly run
    # writes only a few KB, so 5 MB fires rarely — it exists to cap the file if
    # something ever gets chatty again.
    handler = RotatingFileHandler(log_dir / "rabbit_hole.log", maxBytes=5_000_000,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # Third-party INFO chatter (httpx "HTTP Request: ..." etc.) drowns the
    # structured events — keep only their warnings.
    for noisy in ("httpx", "httpcore", "plexapi"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
    return structlog.get_logger()

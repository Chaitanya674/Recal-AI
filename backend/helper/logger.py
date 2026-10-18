"""Central logging: everything goes to the console AND a rotating file.

Usage:   from ..helper.logger import get_logger
         log = get_logger(__name__)
         log.info("ingested %s (%d chunks)", name, n)

Env:     LOG_LEVEL  DEBUG | INFO | WARNING   (default INFO)
         LOG_DIR    where recal.log lives    (default <repo>/logs; file logging
                    is skipped silently on a read-only disk, e.g. some PaaS)

The root logger carries the handlers, so library logs (google clients, httpx,
FastAPI) are captured too; the noisiest ones are capped at INFO so LOG_LEVEL=DEBUG
stays readable. Uvicorn keeps its own console handlers (propagate=False) — the
request middleware in backend.main logs every request here instead."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE = "%Y-%m-%d %H:%M:%S"
_NOISY = ("urllib3", "httpx", "httpcore", "google", "grpc", "asyncio", "watchfiles")
_configured = False


class _RedactingFormatter(logging.Formatter):
    """Secrets never reach the console or recal.log — tracebacks included
    (an HTTP error can carry an API key inside its request URL)."""

    def format(self, record: logging.LogRecord) -> str:
        from .guardrails import redact
        return redact(super().format(record))


def setup_logging() -> None:
    """Idempotent: safe to call from every module via get_logger()."""
    global _configured
    if _configured:
        return
    _configured = True
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    fmt = _RedactingFormatter(_FMT, _DATE)
    root = logging.getLogger()
    root.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    log_dir = Path(os.getenv("LOG_DIR") or Path(__file__).resolve().parents[2] / "logs")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file = RotatingFileHandler(log_dir / "recal.log", maxBytes=5_000_000,
                                   backupCount=3, encoding="utf-8")
        file.setFormatter(fmt)
        root.addHandler(file)
    except OSError:  # read-only container filesystem: stdout-only is fine
        pass
    for name in _NOISY:
        logging.getLogger(name).setLevel(max(level, logging.INFO))


def get_logger(name: str = "recal") -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)

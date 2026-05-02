from __future__ import annotations

import logging
import platform
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .paths import logs_dir

_CONFIGURED = False


def _handler(path: Path, level: int) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    return handler


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    base = logs_dir()
    root.addHandler(_handler(base / "app.log", logging.INFO))
    root.addHandler(_handler(base / "errors.log", logging.ERROR))
    logging.getLogger("voice_input.transcription").addHandler(_handler(base / "transcription.log", logging.INFO))
    _CONFIGURED = True
    log = get_logger("startup")
    log.info("Voice Input Local started")
    log.info("Python: %s", sys.version.replace("\n", " "))
    log.info("Platform: %s", platform.platform())


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    if name.startswith("voice_input"):
        return logging.getLogger(name)
    return logging.getLogger(f"voice_input.{name}")


def log_exception(name: str, message: str, **extra: Any) -> None:
    log = get_logger(name)
    if extra:
        log.exception("%s | %s", message, extra)
    else:
        log.exception(message)

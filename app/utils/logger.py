from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


_CONFIGURED = False


def _level_from_env() -> int:
    level_name = os.getenv("LOG_LEVEL", os.getenv("SMARTLOCKER_LOG_LEVEL", "INFO")).upper()
    return getattr(logging, level_name, logging.INFO)


def _log_file_path() -> Path:
    return Path(os.getenv("SMARTLOCKER_LOG_FILE", "/var/log/smartlocker/device.log"))


def _configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = _level_from_env()
    formatter = logging.Formatter(
        '{"timestamp":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}'
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    try:
        log_path = _log_file_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=int(os.getenv("SMARTLOCKER_LOG_MAX_BYTES", "10485760")),
            backupCount=int(os.getenv("SMARTLOCKER_LOG_BACKUP_COUNT", "5")),
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except Exception:
        root.debug("Rotating file logging unavailable", exc_info=True)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _configure_logging()
    return logging.getLogger(name)

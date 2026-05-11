import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_DIR = Path(__file__).resolve().parent
LOG_FILE = "streaming_agent.log"
ERROR_LOG_FILE = "errors.log"
MAX_LOG_SIZE = 10 * 1024 * 1024
BACKUP_COUNT = 5


class LoggingManager:
    """Centralized logging setup for the streaming agent."""

    _initialized = False

    @classmethod
    def initialize(cls):
        if cls._initialized:
            return

        LOG_DIR.mkdir(parents=True, exist_ok=True)

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)

        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        if not any(isinstance(handler, logging.StreamHandler) for handler in root_logger.handlers):
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(formatter)
            root_logger.addHandler(console_handler)

        log_paths = {
            LOG_FILE: logging.INFO,
            ERROR_LOG_FILE: logging.ERROR,
        }
        existing_files = {
            getattr(handler, "baseFilename", None) for handler in root_logger.handlers
        }

        for filename, level in log_paths.items():
            file_path = str(LOG_DIR / filename)
            if file_path in existing_files:
                continue

            file_handler = RotatingFileHandler(
                filename=file_path,
                maxBytes=MAX_LOG_SIZE,
                backupCount=BACKUP_COUNT,
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)

        cls._initialized = True
        logging.getLogger(__name__).info("Streaming agent logging initialized")

    @staticmethod
    def get_logger(name: str) -> logging.Logger:
        if not LoggingManager._initialized:
            LoggingManager.initialize()
        return logging.getLogger(name)

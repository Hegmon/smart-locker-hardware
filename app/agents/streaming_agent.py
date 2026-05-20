from __future__ import annotations

import threading

from app.streaming_agent.main import StreamingAgent as StreamingRuntime
from app.utils.logger import get_logger


logger = get_logger(__name__)


class StreamingAgent:
    def __init__(self):
        self._agent = StreamingRuntime()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._agent.run_forever, daemon=True, name="streaming-agent")
        self._thread.start()
        logger.info("Streaming agent started")

    def stop(self) -> None:
        try:
            self._agent.stop()
        except Exception:
            logger.debug("Streaming agent stop failed", exc_info=True)
        if self._thread:
            self._thread.join(timeout=10.0)
            self._thread = None
        logger.info("Streaming agent stopped")

from __future__ import annotations

import threading

from app.hardware_agent.config import load_agent_config
from app.hardware_agent.main import WifiUploadAgent
from app.utils.logger import get_logger


logger = get_logger(__name__)


class WifiAgent:
    def __init__(self):
        self._agent: WifiUploadAgent | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._agent = WifiUploadAgent(load_agent_config())
        self._thread = threading.Thread(target=self._agent.start, daemon=True, name="wifi-agent")
        self._thread.start()
        logger.info("WiFi agent started")

    def stop(self) -> None:
        if self._agent:
            self._agent._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("WiFi agent stopped")

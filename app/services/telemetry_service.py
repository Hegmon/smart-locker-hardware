from __future__ import annotations

import threading
import time
from typing import Any

from app.core.mqtt_manager import MQTTManager
from app.deployment.runtime_config import get_int_setting, get_str_setting
from app.utils.logger import get_logger
from app.utils.system_info import telemetry_snapshot


logger = get_logger(__name__)


class TelemetryService:
    """Publishes device telemetry without blocking locker control paths."""

    def __init__(
        self,
        mqtt_manager: MQTTManager,
        *,
        interval_seconds: int | None = None,
        wifi_interface: str | None = None,
    ):
        self.mqtt = mqtt_manager
        self.interval_seconds = max(1, interval_seconds or get_int_setting("TELEMETRY_INTERVAL_SECONDS", 5))
        self.wifi_interface = wifi_interface or get_str_setting("WIFI_INTERFACE", "wlan0")
        self.topic = f"{self.mqtt.topic_prefix}/{self.mqtt.device_id}/telemetry"
        self._running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="telemetry-service")
        self._thread.start()
        logger.info("Telemetry service started interval_seconds=%s topic=%s", self.interval_seconds, self.topic)

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        logger.info("Telemetry service stopped")

    def collect(self) -> dict[str, Any]:
        return telemetry_snapshot(self.mqtt.device_id, wifi_interface=self.wifi_interface)

    def _run(self) -> None:
        while self._running:
            try:
                payload = self.collect()
                published = self.mqtt.publish_json(self.topic, payload, qos=1)
                logger.info(
                    "Telemetry publish %s topic=%s cpu=%s ram=%s disk=%s",
                    "sent" if published else "queued",
                    self.topic,
                    payload.get("cpu_usage"),
                    payload.get("ram_usage"),
                    payload.get("disk_usage"),
                )
            except Exception:
                logger.exception("Telemetry publish failed")
            self._stop_event.wait(self.interval_seconds)

from __future__ import annotations

import threading
from typing import Any

from app.core.mqtt_manager import MQTTManager
from app.deployment.runtime_config import get_int_setting, get_str_setting
from app.utils.logger import get_logger
from app.utils.system_info import network_connected, utc_timestamp


logger = get_logger(__name__)


class HealthMonitor:
    """Watchdog-style monitor for process, network, and MQTT liveness."""

    def __init__(
        self,
        mqtt_manager: MQTTManager,
        *,
        interval_seconds: int | None = None,
    ):
        self.mqtt = mqtt_manager
        self.interval_seconds = max(5, interval_seconds or get_int_setting("HEALTH_MONITOR_INTERVAL_SECONDS", 15))
        self.topic = f"{self.mqtt.topic_prefix}/{self.mqtt.device_id}/health"
        self._running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_network_connected: bool | None = None
        self._last_mqtt_status: str | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="health-monitor")
        self._thread.start()
        logger.info("Health monitor started interval_seconds=%s", self.interval_seconds)

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        logger.info("Health monitor stopped")

    def snapshot(self) -> dict[str, Any]:
        network_ok = network_connected()
        mqtt_status = self.mqtt.mqtt_status()
        return {
            "device_id": self.mqtt.device_id,
            "status": "ok" if network_ok and mqtt_status == "connected" else "degraded",
            "network_connected": network_ok,
            "mqtt_status": mqtt_status,
            "mqtt_connected": self.mqtt.is_connected(),
            "service": get_str_setting("SMARTLOCKER_SERVICE_NAME", "smart-locker-device"),
            "timestamp": utc_timestamp(),
        }

    def _run(self) -> None:
        while self._running:
            try:
                payload = self.snapshot()
                self._log_transitions(payload)
                self.mqtt.publish_json(self.topic, payload, qos=1)
            except Exception:
                logger.exception("Health monitor iteration failed")
            self._stop_event.wait(self.interval_seconds)

    def _log_transitions(self, payload: dict[str, Any]) -> None:
        network_ok = bool(payload.get("network_connected"))
        mqtt_status = str(payload.get("mqtt_status"))
        if self._last_network_connected is None or self._last_network_connected != network_ok:
            logger.info("Network connectivity changed connected=%s", network_ok)
            self._last_network_connected = network_ok
        if self._last_mqtt_status is None or self._last_mqtt_status != mqtt_status:
            logger.info("Observed MQTT status=%s", mqtt_status)
            self._last_mqtt_status = mqtt_status

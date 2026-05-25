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
        self.mqtt.add_connect_listener(self.publish_once)

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
        return self._normalize_payload(
            telemetry_snapshot(self.mqtt.device_id, wifi_interface=self.wifi_interface)
        )

    def publish_once(self) -> bool:
        try:
            payload = self.collect()
            if not self._has_required_metrics(payload):
                logger.error("Telemetry payload missing required metrics; publish skipped payload=%s", payload)
                return False
            published = self.mqtt.publish_json(self.topic, payload, qos=1)
            logger.info(
                "Telemetry publish %s topic=%s cpu=%s ram=%s disk=%s",
                "sent" if published else "queued",
                self.topic,
                payload.get("cpu_usage"),
                payload.get("ram_usage"),
                payload.get("disk_usage"),
            )
            return published
        except Exception:
            logger.exception("Telemetry publish failed")
            return False

    def _run(self) -> None:
        while self._running:
            self.publish_once()
            self._stop_event.wait(self.interval_seconds)

    def _normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "device_id": self.mqtt.device_id,
            "cpu_usage": 0.0,
            "ram_usage": 0.0,
            "disk_usage": 0.0,
            "cpu_temperature": None,
            "uptime_seconds": 0,
            "network_connected": False,
            "local_ip": "",
            "wifi_ssid": "",
            "timestamp": "",
        }
        normalized = {**defaults, **payload}
        for key in ("cpu_usage", "ram_usage", "disk_usage"):
            normalized[key] = self._float_or_default(normalized.get(key), 0.0)
        normalized["uptime_seconds"] = self._int_or_default(normalized.get("uptime_seconds"), 0)
        normalized["network_connected"] = bool(normalized.get("network_connected"))
        normalized["local_ip"] = str(normalized.get("local_ip") or "")
        normalized["wifi_ssid"] = str(normalized.get("wifi_ssid") or "")
        normalized["timestamp"] = str(normalized.get("timestamp") or "")
        return normalized

    def _has_required_metrics(self, payload: dict[str, Any]) -> bool:
        required = ("cpu_usage", "ram_usage", "disk_usage", "uptime_seconds", "timestamp")
        return all(payload.get(key) is not None and payload.get(key) != "" for key in required)

    @staticmethod
    def _float_or_default(value: Any, default: float) -> float:
        try:
            return round(float(value), 1)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _int_or_default(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

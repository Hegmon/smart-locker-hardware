from __future__ import annotations

import os
import signal
import shutil
import sys
import threading
import time
from typing import Any
import psutil
from app.core.mqtt_manager import MQTTManager
from app.utils.logger import get_logger
logger = get_logger(__name__)


class TelemetryAgent:
    def __init__(self, mqtt_manager: MQTTManager, *, interval_seconds: int = 30):
        self.mqtt = mqtt_manager
        self.interval_seconds = max(1, interval_seconds)
        self.topic = f"devices/{self.mqtt.device_id}/telemetry"
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="telemetry-agent")
        self._thread.start()
        logger.info("Telemetry agent started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("Telemetry agent stopped")

    def _run(self) -> None:
        while self._running:
            try:
                self.mqtt.publish_json(self.topic, self.collect(), qos=1)
            except Exception:
                logger.exception("Telemetry publish failed")
            self._sleep()

    def _sleep(self) -> None:
        deadline = time.monotonic() + self.interval_seconds
        while self._running and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))

    def collect(self) -> dict[str, Any]:
        disk = shutil.disk_usage("/")
        return {
            "cpu": round(psutil.cpu_percent(interval=None), 1),
            "ram": round(psutil.virtual_memory().percent, 1),
            "disk": round((disk.used / disk.total) * 100, 1) if disk.total else 0,
            "temperature": self._cpu_temperature(),
            "uptime": int(time.time() - psutil.boot_time()),
            "timestamp": int(time.time()),
        }

    def _cpu_temperature(self) -> float | None:
        thermal_path = "/sys/class/thermal/thermal_zone0/temp"
        try:
            if os.path.exists(thermal_path):
                with open(thermal_path, "r", encoding="utf-8") as temp_file:
                    return round(int(temp_file.read().strip()) / 1000.0, 1)
        except Exception:
            logger.debug("CPU temperature file could not be read", exc_info=True)

        try:
            temperatures = psutil.sensors_temperatures()
        except Exception:
            return None
        for entries in temperatures.values():
            for entry in entries:
                if entry.current is not None:
                    return round(float(entry.current), 1)
        return None


class HeartbeatAgent:
    def __init__(self, mqtt_manager: MQTTManager, *, interval_seconds: int = 60):
        self.mqtt = mqtt_manager
        self.interval_seconds = max(1, interval_seconds)
        self.topic = f"devices/{self.mqtt.device_id}/heartbeat"
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="heartbeat-agent")
        self._thread.start()
        logger.info("Heartbeat agent started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("Heartbeat agent stopped")

    def _run(self) -> None:
        while self._running:
            try:
                self.mqtt.publish_json(
                    self.topic,
                    {"alive": True, "timestamp": int(time.time())},
                    qos=1,
                )
            except Exception:
                logger.exception("Heartbeat publish failed")
            deadline = time.monotonic() + self.interval_seconds
            while self._running and time.monotonic() < deadline:
                time.sleep(min(0.5, deadline - time.monotonic()))


def run_telemetry_forever() -> None:
    from app.core.mqtt_manager import get_shared_mqtt_manager

    mqtt = get_shared_mqtt_manager()
    agent = TelemetryAgent(mqtt)
    stopped = threading.Event()

    def _stop(signum=None, frame=None):
        logger.info("Telemetry agent stop requested")
        stopped.set()
        agent.stop()
        mqtt.stop(publish_offline=True)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    mqtt.start()
    agent.start()
    try:
        while not stopped.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        _stop()


def run_heartbeat_forever() -> None:
    from app.core.mqtt_manager import get_shared_mqtt_manager

    mqtt = get_shared_mqtt_manager()
    agent = HeartbeatAgent(mqtt)
    stopped = threading.Event()

    def _stop(signum=None, frame=None):
        logger.info("Heartbeat agent stop requested")
        stopped.set()
        agent.stop()
        mqtt.stop(publish_offline=True)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    mqtt.start()
    agent.start()
    try:
        while not stopped.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        _stop()


if __name__ == "__main__":
    mode = sys.argv[1].strip().lower() if len(sys.argv) > 1 else "telemetry"
    if mode == "heartbeat":
        run_heartbeat_forever()
    else:
        run_telemetry_forever()

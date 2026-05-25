from __future__ import annotations

import signal
import sys
import threading
import time
from app.core.mqtt_manager import MQTTManager
from app.services.telemetry_service import TelemetryService
from app.utils.logger import get_logger
from app.utils.system_info import utc_timestamp
logger = get_logger(__name__)


class TelemetryAgent(TelemetryService):
    """Backward-compatible name for the production telemetry service."""


class HeartbeatAgent:
    def __init__(self, mqtt_manager: MQTTManager, *, interval_seconds: int = 60):
        self.mqtt = mqtt_manager
        self.interval_seconds = max(1, interval_seconds)
        self.topic = f"{self.mqtt.topic_prefix}/{self.mqtt.device_id}/heartbeat"
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
                    {"device_id": self.mqtt.device_id, "alive": True, "timestamp": utc_timestamp()},
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

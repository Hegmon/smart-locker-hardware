from __future__ import annotations

import signal
import threading
import time

from app.core.mqtt_manager import MQTTManager
from app.utils.logger import get_logger


logger = get_logger(__name__)


class ControlAgent:
    def __init__(self, mqtt_manager: MQTTManager):
        self.mqtt = mqtt_manager
        self.topic = f"devices/{self.mqtt.device_id}/commands"
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self.mqtt.subscribe(self.topic, self._handle_command, qos=1)
        self._started = True
        logger.info("Control agent subscribed to %s", self.topic)

    def stop(self) -> None:
        self._started = False

    def _handle_command(self, topic: str, payload: bytes) -> None:
        command = self.mqtt.loads(payload)
        logger.info("Received device command on %s: %s", topic, command)


def main() -> None:
    from app.core.mqtt_manager import get_shared_mqtt_manager

    mqtt = get_shared_mqtt_manager()
    agent = ControlAgent(mqtt)
    stopped = threading.Event()

    def _stop(signum=None, frame=None):
        logger.info("Control agent stop requested")
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
    main()

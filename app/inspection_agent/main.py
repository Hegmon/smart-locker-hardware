from __future__ import annotations

"""Entry point for the Smart Locker inspection agent."""

import signal
import time

from app.core.mqtt_manager import get_shared_mqtt_manager
from app.inspection_agent.manager import InspectionAgentManager
from app.inspection_agent.mqtt.inspection_subscriber import InspectionSubscriber
from app.utils.logger import get_logger


logger = get_logger(__name__)


class InspectionAgentRuntime:
    """Long-running MQTT inspection runtime with automatic reconnect support."""

    def __init__(self) -> None:
        self.mqtt = get_shared_mqtt_manager()
        self.manager = InspectionAgentManager(device_id=self.mqtt.device_id)
        self.subscriber = InspectionSubscriber(
            mqtt_manager=self.mqtt,
            manager=self.manager,
            device_id=self.mqtt.device_id,
        )
        self._running = False

    def start(self) -> None:
        logger.info("Starting inspection agent device_id=%s", self.mqtt.device_id)
        self.mqtt.start()
        self.subscriber.start()
        self._running = True
        logger.info("Inspection agent started and subscribed to inspection requests")

    def stop(self) -> None:
        if not self._running:
            return
        logger.info("Stopping inspection agent")
        self._running = False
        try:
            self.subscriber.stop()
        except Exception:
            logger.exception("Inspection subscriber shutdown failed")
        try:
            self.mqtt.stop(publish_offline=True)
        except Exception:
            logger.exception("MQTT shutdown failed")
        logger.info("Inspection agent stopped")

    def run_forever(self) -> None:
        self.start()
        try:
            while self._running:
                time.sleep(1)
        finally:
            self.stop()


def main() -> None:
    runtime = InspectionAgentRuntime()

    def _handle_signal(signum, _frame) -> None:
        logger.info("Signal received: %s", signum)
        runtime.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        runtime.run_forever()
    except KeyboardInterrupt:
        runtime.stop()
    except Exception:
        logger.exception("Inspection agent failed")
        runtime.stop()
        raise


if __name__ == "__main__":
    main()

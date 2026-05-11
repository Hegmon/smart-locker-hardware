import signal
import sys
import time

from app.core.config import MQTT_HOST, MQTT_PASSWORD, MQTT_PORT, MQTT_USERNAME
from app.streaming_agent.health_monitor import HealthMonitor
from app.streaming_agent.hot_plug_monitor import HotPlugMonitor
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager
from app.streaming_agent.mqtt_publisher import MQTTPublisher
from app.streaming_agent.streaming_manager import StreamingManager


logger = LoggingManager.get_logger(__name__)


class StreamingAgent:
    def __init__(self):
        self.stream_manager = None
        self.health_monitor = None
        self.hot_plug_monitor = None
        self.mqtt_publisher = None
        self.running = False

    def initialize(self):
        logger.info("Initializing streaming agent")
        self.stream_manager = StreamingManager()
        self.stream_manager.initialize()
        self.health_monitor = HealthMonitor(stream_registry=self.stream_manager.streams)
        self.hot_plug_monitor = HotPlugMonitor(stream_manager=self.stream_manager)
        self.mqtt_publisher = MQTTPublisher(
            stream_manager=self.stream_manager,
            health_monitor=self.health_monitor,
            broker_host="69.62.125.223",
            broker_port=8554,
            username=MQTT_USERNAME,
            password=MQTT_PASSWORD,
        )
        logger.info("Streaming agent initialized successfully")

    def start(self):
        logger.info("Starting streaming agent")
        self.running = True
        self.stream_manager.start_all()
        self.health_monitor.start()
        self.mqtt_publisher.start()
        self.hot_plug_monitor.start()
        logger.info("Streaming agent started successfully")

    def stop(self):
        logger.info("Stopping streaming agent")
        self.running = False
        if self.hot_plug_monitor:
            self.hot_plug_monitor.stop()
        if self.mqtt_publisher:
            self.mqtt_publisher.stop()
        if self.health_monitor:
            self.health_monitor.stop()
        if self.stream_manager:
            self.stream_manager.stop_all()
        logger.info("Streaming agent stopped successfully")

    def run_forever(self):
        self.initialize()
        self.start()

        while self.running:
            time.sleep(1)


agent = StreamingAgent()


def signal_handler(sig, frame):
    logger.info("Signal received: %s", sig)
    agent.stop()
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


if __name__ == "__main__":
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received, stopping agent")
        agent.stop()
        sys.exit(0)
    except Exception:
        logger.exception("Unexpected streaming agent error")
        agent.stop()
        sys.exit(1)

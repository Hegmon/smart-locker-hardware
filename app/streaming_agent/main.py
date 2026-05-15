import signal
import sys
import threading
import time

from app.core.config import MQTT_HOST, MQTT_PASSWORD, MQTT_PORT, MQTT_USERNAME
from app.streaming_agent.detection.person_detector import PersonDetector
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
        self.person_detector = None
        self.keyboard_thread = None
        self.running = False
        self._stopping = False
        self._stop_lock = threading.Lock()

    def initialize(self):
        logger.info("Initializing streaming agent")
        self.stream_manager = StreamingManager()
        self.stream_manager.initialize()
        self.person_detector = PersonDetector(self.stream_manager.get_frame_buffer("internal"))
        self.health_monitor = HealthMonitor(stream_registry=self.stream_manager.streams)
        self.hot_plug_monitor = HotPlugMonitor(stream_manager=self.stream_manager)
        self.mqtt_publisher = MQTTPublisher(
            stream_manager=self.stream_manager,
            health_monitor=self.health_monitor,
            broker_host="69.62.125.223",
            broker_port=1883,
            username=MQTT_USERNAME,
            password=MQTT_PASSWORD,
        )
        logger.info("Streaming agent initialized successfully")

    def start(self):
        logger.info("Starting streaming agent")
        self.running = True
        self.stream_manager.start_all()
        if self.person_detector:
            self.person_detector.start()
        self.health_monitor.start()
        self.mqtt_publisher.start()
        self.hot_plug_monitor.start()
        self._start_keyboard_listener()
        logger.info("Streaming agent started successfully")
        logger.info("Press Ctrl+C or type q then Enter to stop the streaming agent")

    def stop(self):
        with self._stop_lock:
            if self._stopping:
                logger.info("Streaming agent stop already in progress")
                return
            self._stopping = True
            self.running = False

        logger.info("Stopping streaming agent")
        try:
            if self.hot_plug_monitor:
                self.hot_plug_monitor.stop()
            if self.mqtt_publisher:
                self.mqtt_publisher.stop()
            if self.health_monitor:
                self.health_monitor.stop()
            if self.person_detector:
                self.person_detector.stop()
            if self.stream_manager:
                self.stream_manager.stop_all()
        finally:
            logger.info("Streaming agent stopped successfully")

    def run_forever(self):
        self.initialize()
        self.start()

        while self.running:
            time.sleep(1)

    def _start_keyboard_listener(self):
        if not sys.stdin or not sys.stdin.isatty() or self.keyboard_thread:
            return

        self.keyboard_thread = threading.Thread(
            target=self._keyboard_loop,
            daemon=True,
            name="streaming-keyboard-listener",
        )
        self.keyboard_thread.start()

    def _keyboard_loop(self):
        while self.running:
            try:
                command = sys.stdin.readline()
            except Exception:
                return
            if not command:
                return
            if command.strip().lower() in {"q", "quit", "exit", "stop"}:
                logger.info("Keyboard stop requested")
                self.stop()
                return


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

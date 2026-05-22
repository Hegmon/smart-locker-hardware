import threading
import time

from app.core.mqtt_manager import MQTTManager, get_shared_mqtt_manager
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)


class MQTTPublisher:
    """Streaming MQTT publisher backed by the shared device MQTT manager."""

    def __init__(
        self,
        stream_manager,
        health_monitor,
        broker_host=None,
        broker_port=None,
        username=None,
        password=None,
        heartbeat_interval=10,
        reconnect_interval=5,
        mqtt_manager: MQTTManager | None = None,
    ):
        self.mqtt = mqtt_manager or get_shared_mqtt_manager()
        self.device_id = self.mqtt.device_id
        self.stream_manager = stream_manager
        self.health_monitor = health_monitor
        self.heartbeat_interval = heartbeat_interval
        self.reconnect_interval = reconnect_interval

        self.running = False
        self.connected = False
        self._ever_connected = False
        self.thread = None
        self.lock = threading.Lock()

        self.mqtt.add_connect_listener(self._on_connect)
        self.mqtt.add_disconnect_listener(self._on_disconnect)

    def _on_connect(self):
        was_connected = self.connected
        self.connected = True
        logger.info(
            "Streaming publisher using shared MQTT broker at %s:%s",
            self.mqtt.config.host,
            self.mqtt.config.port,
        )
        if self.running and self._ever_connected and not was_connected:
            logger.info("MQTT reconnected after network loss; restarting streams")
            try:
                self.stream_manager.restart_all(reason="MQTT reconnected after network loss")
            except Exception:
                logger.exception("Failed to restart streams after MQTT reconnect")
        self._ever_connected = True

    def _on_disconnect(self):
        self.connected = False
        if self.running:
            logger.warning("Streaming publisher observed shared MQTT disconnect")

    def start(self):
        with self.lock:
            if self.running:
                logger.info("MQTT publisher is already running")
                return

            logger.info("Starting streaming MQTT publisher")
            self.running = True
            self.mqtt.start()
            self.connected = self.mqtt.is_connected()
            self.thread = threading.Thread(target=self._publish_loop, daemon=True, name="streaming-mqtt-publisher")
            self.thread.start()

    def stop(self):
        with self.lock:
            if not self.running:
                logger.info("MQTT publisher is not running")
                return

            logger.info("Stopping streaming MQTT publisher")
            self.running = False

        if self.thread:
            self.thread.join(timeout=self.heartbeat_interval + 1)
            self.thread = None

        self.connected = False
        logger.info("MQTT publisher stopped successfully")

    def _publish_loop(self):
        while self.running:
            try:
                if not self.mqtt.is_connected():
                    logger.warning(
                        "Shared MQTT broker %s:%s unavailable; publishes will queue",
                        self.mqtt.config.host,
                        self.mqtt.config.port,
                    )

                self.publish_stream_status()
            except Exception:
                logger.exception("MQTT publishing error")

            time.sleep(self.heartbeat_interval)

    def publish_device_status(self):
        topic = f"devices/{self.device_id}/heartbeat"
        payload = {
            "alive": True,
            "timestamp": int(time.time()),
        }
        self._publish(topic, payload)

    def publish_stream_status(self):
        topic = f"devices/{self.device_id}/stream/status"
        payload = self.stream_manager.get_stream_status()
        if isinstance(payload, dict) and self.health_monitor:
            payload.setdefault("health", self.health_monitor.get_metrics())
            payload.setdefault("timestamp", int(time.time()))
        self._publish(topic, payload)
        if isinstance(payload, dict):
            for role in ("internal", "external"):
                role_payload = payload.get(role)
                if isinstance(role_payload, dict):
                    self._publish(f"devices/{self.device_id}/stream/{role}/status", role_payload)

    def publish_health_metrics(self):
        self.publish_stream_status()

    def _publish(self, topic, payload):
        retain = "/stream/" in topic or topic.endswith("/stream/status")
        published = self.mqtt.publish_json(topic, payload, qos=1, retain=retain)
        if published:
            logger.info("Published MQTT message to topic %s", topic)
        else:
            logger.info("Queued MQTT message to topic %s", topic)

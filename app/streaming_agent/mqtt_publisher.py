import json
import threading
import time

import paho.mqtt.client as mqtt

from app.streaming_agent.config_loader import get_device_id
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)


class MQTTPublisher:
    def __init__(
        self,
        stream_manager,
        health_monitor,
        broker_host="localhost",
        broker_port=1883,
        heartbeat_interval=10,
    ):
        self.device_id = get_device_id()
        self.stream_manager = stream_manager
        self.health_monitor = health_monitor
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.heartbeat_interval = heartbeat_interval
        self.client = mqtt.Client(client_id=f"streaming-agent-{self.device_id}")
        self.running = False
        self.thread = None
        self.lock = threading.Lock()
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        logger.info(
            "Connected to MQTT broker at %s:%s with reason code %s",
            self.broker_host,
            self.broker_port,
            reason_code,
        )

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        logger.warning("Disconnected from MQTT broker with reason code %s", reason_code)

    def start(self):
        with self.lock:
            if self.running:
                logger.info("MQTT publisher is already running")
                return

            logger.info("Starting MQTT publisher")
            self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            self.client.loop_start()
            self.running = True
            self.thread = threading.Thread(target=self._publish_loop, daemon=True, name="mqtt-publisher")
            self.thread.start()

    def stop(self):
        with self.lock:
            if not self.running:
                logger.info("MQTT publisher is not running")
                return

            logger.info("Stopping MQTT publisher")
            self.running = False
            self.client.loop_stop()
            self.client.disconnect()
            if self.thread:
                self.thread.join(timeout=self.heartbeat_interval + 1)
                self.thread = None
            logger.info("MQTT publisher stopped successfully")

    def _publish_loop(self):
        while self.running:
            try:
                self.publish_device_status()
                self.publish_stream_status()
                self.publish_health_metrics()
            except Exception:
                logger.exception("MQTT publishing error")
            time.sleep(self.heartbeat_interval)

    def publish_device_status(self):
        topic = f"devices/{self.device_id}/status"
        payload = {
            "device_id": self.device_id,
            "status": "online",
            "timestamp": int(time.time()),
        }
        self._publish(topic, payload)

    def publish_stream_status(self):
        topic = f"devices/{self.device_id}/streams"
        payload = self.stream_manager.get_stream_status()
        self._publish(topic, payload)

    def publish_health_metrics(self):
        topic = f"devices/{self.device_id}/health"
        payload = self.health_monitor.get_metrics()
        self._publish(topic, payload)

    def _publish(self, topic, payload):
        message = json.dumps(payload)
        result = self.client.publish(topic, message, qos=1)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info("Published MQTT message to topic %s", topic)
        else:
            logger.error("Failed to publish MQTT message to topic %s with rc=%s", topic, result.rc)

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
        broker_host="69.62.125.223",
        broker_port=8554,
        username=None,
        password=None,
        heartbeat_interval=10,
        reconnect_interval=5,
    ):
        self.device_id = get_device_id()
        self.stream_manager = stream_manager
        self.health_monitor = health_monitor
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.username = username
        self.password = password
        self.heartbeat_interval = heartbeat_interval
        self.reconnect_interval = reconnect_interval

        self.client = mqtt.Client(client_id=f"streaming-agent-{self.device_id}")
        if self.username:
            self.client.username_pw_set(self.username, self.password)

        self.running = False
        self.connected = False
        self.thread = None
        self.lock = threading.Lock()

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = reason_code == 0
        if self.connected:
            logger.info(
                "Connected to MQTT broker at %s:%s",
                self.broker_host,
                self.broker_port,
            )
        else:
            logger.warning(
                "MQTT connection failed to %s:%s with reason code %s",
                self.broker_host,
                self.broker_port,
                reason_code,
            )

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        self.connected = False
        if self.running:
            logger.warning("Disconnected from MQTT broker with reason code %s", reason_code)
        else:
            logger.info("MQTT client disconnected")

    def start(self):
        with self.lock:
            if self.running:
                logger.info("MQTT publisher is already running")
                return

            logger.info("Starting MQTT publisher for broker %s:%s", self.broker_host, self.broker_port)
            self.running = True
            self.client.loop_start()
            self._connect_async()
            self.thread = threading.Thread(target=self._publish_loop, daemon=True, name="mqtt-publisher")
            self.thread.start()

    def stop(self):
        with self.lock:
            if not self.running:
                logger.info("MQTT publisher is not running")
                return

            logger.info("Stopping MQTT publisher")
            self.running = False

        if self.thread:
            self.thread.join(timeout=self.heartbeat_interval + 1)
            self.thread = None

        self.client.loop_stop()
        try:
            self.client.disconnect()
        except Exception:
            logger.exception("Error while disconnecting MQTT client")

        self.connected = False
        logger.info("MQTT publisher stopped successfully")

    def _connect_async(self):
        try:
            self.client.connect_async(self.broker_host, self.broker_port, keepalive=60)
            logger.info("MQTT async connect requested for %s:%s", self.broker_host, self.broker_port)
        except Exception:
            logger.exception("Failed to schedule MQTT async connect")

    def _publish_loop(self):
        while self.running:
            try:
                if not self.connected:
                    logger.warning(
                        "MQTT broker %s:%s unavailable, retrying in background",
                        self.broker_host,
                        self.broker_port,
                    )
                    try:
                        self.client.reconnect()
                    except Exception:
                        logger.debug("MQTT reconnect attempt failed", exc_info=True)
                        self._connect_async()
                    time.sleep(self.reconnect_interval)
                    continue

                self.publish_device_status()
                self.publish_stream_status()
                self.publish_health_metrics()
            except Exception:
                logger.exception("MQTT publishing error")

            time.sleep(self.heartbeat_interval)

    def publish_device_status(self):
      topic = f"devices/{self.device_uuid}/heartbeat"
      payload = {
        "device_id": self.device_id,
        "status": "online",
        "timestamp": int(time.time()),
      }  
      self._publish(topic, payload)

    def publish_stream_status(self):
       topic = f"devices/{self.device_uuid}/events/state"
       payload = self.stream_manager.get_stream_status()
       self._publish(topic, payload)

    def publish_health_metrics(self):
      topic = f"devices/{self.device_uuid}/services/health/response"
      payload = self.health_monitor.get_metrics()
      self._publish(topic, payload)

    def _publish(self, topic, payload):
        if not self.connected:
            logger.warning("Skipping MQTT publish to %s because broker is not connected", topic)
            return

        message = json.dumps(payload)
        result = self.client.publish(topic, message, qos=1)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info("Published MQTT message to topic %s", topic)
        else:
            logger.error("Failed to publish MQTT message to topic %s with rc=%s", topic, result.rc)

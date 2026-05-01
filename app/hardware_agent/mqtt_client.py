from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

import paho.mqtt.client as mqtt

from app.utils.logger import get_logger


logger = get_logger(__name__)


class MqttClient:
    def __init__(
        self,
        host: str,
        port: int,
        client_id: str,
        keepalive: int = 60,
        username: str | None = None,
        password: str | None = None,
    ):
        self.host = host
        self.port = port
        self.client_id = f"qbox_{client_id}"
        self.keepalive = keepalive
        self.username = username
        self.password = password

        self._running = True
        self._connected = False
        self._connection_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._processed_lock = threading.Lock()
        self._processed_commands: list[str] = []
        self._processed_command_set: set[str] = set()
        self._fallback_notified = False
        self._pending_messages: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._max_pending_messages = 100

        self._command_handler: Callable[[dict[str, Any], str], dict[str, Any] | None] | None = None
        self._ble_fallback_handler: Callable[[], None] | None = None

        self.client = mqtt.Client(client_id=self.client_id, clean_session=True)
        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    def connect(self):
        try:
            self.client.loop_start()
            self.client.connect_async(self.host, self.port, self.keepalive)
        except Exception as exc:
            logger.warning("MQTT async connect failed, falling back to background connect: %s", exc)

            def _blocking_connect():
                try:
                    self.client.connect(self.host, self.port, self.keepalive)
                except Exception as inner_exc:
                    logger.error("MQTT blocking connect failed: %s", inner_exc)

            threading.Thread(target=_blocking_connect, daemon=True, name="mqtt-connect").start()

        threading.Thread(target=self._watchdog, daemon=True, name="mqtt-watchdog").start()

    def disconnect(self):
        self._running = False
        self.client.loop_stop()
        self.client.disconnect()

    def is_connected(self) -> bool:
        with self._connection_lock:
            return self._connected

    def publish(self, topic: str, payload: dict[str, Any]) -> bool:
        if not self.is_connected():
            self._queue_publish(topic, payload)
            return False

        try:
            return self._publish_now(topic, payload)
        except Exception:
            logger.exception("MQTT publish failed for topic %s", topic)
            self._queue_publish(topic, payload)
            return False

    def register_command_handler(self, handler: Callable[[dict[str, Any], str], dict[str, Any] | None]):
        self._command_handler = handler

    def register_ble_fallback_handler(self, handler: Callable[[], None]):
        self._ble_fallback_handler = handler

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            logger.warning("MQTT connection failed with rc=%s", rc)
            return

        with self._connection_lock:
            self._connected = True
            self._fallback_notified = False

        client.subscribe("devices/+/services/+/request", qos=1)
        logger.info("MQTT connected to %s:%s", self.host, self.port)
        self._flush_pending_messages()

    def _on_disconnect(self, client, userdata, rc):
        with self._connection_lock:
            was_connected = self._connected
            self._connected = False

        if was_connected:
            logger.warning("MQTT disconnected with rc=%s", rc)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            logger.exception("MQTT payload decode failed for topic %s", msg.topic)
            return

        topic_parts = msg.topic.split("/")
        if len(topic_parts) < 5:
            logger.warning("MQTT topic ignored: %s", msg.topic)
            return

        device_id = topic_parts[1]
        service = topic_parts[3]
        command_id = payload.get("command_id")

        if command_id and self._is_duplicate_command(command_id):
            return

        if self._command_handler is None:
            return

        try:
            response = self._command_handler(payload, msg.topic)
            if response is None:
                return

            response_topic = f"devices/{device_id}/services/{service}/response"
            self.publish(
                response_topic,
                {
                    "command_id": command_id,
                    "service": service,
                    "result": response,
                },
            )
        except Exception:
            logger.exception("MQTT command handler failed for topic %s", msg.topic)

    def _watchdog(self):
        while self._running:
            if not self.is_connected():
                try:
                    self.client.reconnect()
                except Exception:
                    try:
                        self.client.connect_async(self.host, self.port, self.keepalive)
                    except Exception as exc:
                        logger.warning("MQTT reconnect retry failed: %s", exc)

                    if self._ble_fallback_handler and not self._fallback_notified:
                        self._fallback_notified = True
                        try:
                            self._ble_fallback_handler()
                        except Exception:
                            logger.exception("MQTT fallback handler failed")

            time.sleep(5)

    def _is_duplicate_command(self, command_id: str) -> bool:
        with self._processed_lock:
            if command_id in self._processed_command_set:
                return True

            self._processed_commands.append(command_id)
            self._processed_command_set.add(command_id)

            if len(self._processed_commands) > 1000:
                while len(self._processed_commands) > 500:
                    expired = self._processed_commands.pop(0)
                    self._processed_command_set.discard(expired)

            return False

    def wait_until_connected(self, timeout_seconds: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.is_connected():
                return True
            time.sleep(0.1)
        return self.is_connected()

    def _publish_now(self, topic: str, payload: dict[str, Any]) -> bool:
        result = self.client.publish(topic, json.dumps(payload), qos=1)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.warning("MQTT publish returned rc=%s for topic %s", result.rc, topic)
            return False
        return True

    def _queue_publish(self, topic: str, payload: dict[str, Any]):
        with self._pending_lock:
            if topic in self._pending_messages:
                self._pending_messages.pop(topic)
            self._pending_messages[topic] = payload

            while len(self._pending_messages) > self._max_pending_messages:
                self._pending_messages.popitem(last=False)
        logger.info("MQTT queued publish while disconnected: %s", topic)

    def _flush_pending_messages(self):
        while True:
            with self._pending_lock:
                if not self._pending_messages:
                    return
                topic, payload = self._pending_messages.popitem(last=False)

            try:
                published = self._publish_now(topic, payload)
            except Exception:
                logger.exception("MQTT pending publish failed for topic %s", topic)
                published = False

            if not published:
                self._queue_publish(topic, payload)
                return

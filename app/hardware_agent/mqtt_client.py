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
        device_uuid: str | None = None,
        strict_device_uuid: bool = False,
        keepalive: int = 60,
        username: str | None = None,
        password: str | None = None,
    ):
        self.host = host
        self.port = port
        self.device_uuid = str(device_uuid or client_id).strip()
        self.strict_device_uuid = strict_device_uuid
        self.client_id = f"qbox_{client_id}"
        self.keepalive = keepalive
        self.username = username
        self.password = password

        self._running = True
        self._connected = False
        self._connection_lock = threading.Lock()
        self._reconnect_lock = threading.Lock()
        self._handler_lock = threading.RLock()
        self._pending_lock = threading.Lock()
        self._processed_lock = threading.Lock()
        self._watchdog_wake = threading.Event()
        self._connect_attempt_deadline = 0.0
        self._processed_commands: list[str] = []
        self._processed_command_set: set[str] = set()
        self._fallback_notified = False
        self._pending_messages: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._max_pending_messages = 100

        self._command_handler: Callable[[dict[str, Any], str], dict[str, Any] | None] | None = None
        self._ble_fallback_handler: Callable[[], None] | None = None

        self.client = mqtt.Client(client_id=self.client_id, clean_session=True)
        try:
            self.client.reconnect_delay_set(min_delay=1, max_delay=2)
        except Exception:
            logger.debug("MQTT reconnect delay configuration skipped", exc_info=True)
        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    def connect(self):
        try:
            self.client.loop_start()
            self.client.connect(self.host, self.port, self.keepalive)
        except Exception as exc:
            logger.warning("MQTT initial connect failed, falling back to background connect: %s", exc)

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

    def ensure_connected(self, timeout_seconds: float = 5.0, *, force_reconnect: bool = False) -> bool:
        if self.is_connected() and not force_reconnect:
            return True

        timeout_seconds = max(0.5, timeout_seconds)
        with self._reconnect_lock:
            if self.is_connected() and not force_reconnect:
                return True
            now = time.monotonic()
            attempt_active = now < self._connect_attempt_deadline and not force_reconnect
            if attempt_active:
                logger.info("MQTT connect attempt already in progress for %s:%s", self.host, self.port)
            else:
                self._connect_attempt_deadline = now + timeout_seconds
                self._start_connect_attempt(force_reconnect=force_reconnect)

        self._watchdog_wake.set()
        connected = self.wait_until_connected(timeout_seconds=timeout_seconds)
        if connected:
            return True

        if time.monotonic() >= self._connect_attempt_deadline:
            with self._reconnect_lock:
                if not self.is_connected() and time.monotonic() >= self._connect_attempt_deadline:
                    self._connect_attempt_deadline = 0.0
        return self.is_connected()

    def _start_connect_attempt(self, *, force_reconnect: bool = False) -> None:
        if force_reconnect:
            with self._connection_lock:
                self._connected = False
            try:
                self.client.disconnect()
            except Exception:
                logger.debug("MQTT disconnect before refresh failed", exc_info=True)
        logger.info(
            "MQTT %s requested for %s:%s",
            "refresh" if force_reconnect else "reconnect",
            self.host,
            self.port,
        )
        try:
            self.client.reconnect()
        except Exception as reconnect_exc:
            logger.debug("MQTT reconnect failed, trying fresh connect: %s", reconnect_exc)
            try:
                self.client.connect(self.host, self.port, self.keepalive)
            except Exception as exc:
                logger.warning("MQTT immediate reconnect failed: %s", exc)

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
        with self._reconnect_lock:
            self._connect_attempt_deadline = 0.0

        if self.device_uuid:
            client.subscribe(f"devices/{self.device_uuid}/services/+/request", qos=1)
        client.subscribe("devices/+/services/+/request", qos=1)
        client.subscribe("hardware_agent/request/+", qos=1)
        logger.info("MQTT connected to %s:%s", self.host, self.port)
        self._flush_pending_messages()

    def _on_disconnect(self, client, userdata, rc):
        self._mark_disconnected()
        logger.warning("MQTT disconnected with rc=%s", rc)
        self._watchdog_wake.set()

    def _mark_disconnected(self):
        with self._connection_lock:
            self._connected = False

    def _on_message(self, client, userdata, msg):
        payload = self._decode_payload(msg.topic, msg.payload)
        request = self._request_metadata(msg.topic, payload)
        if request is None:
            logger.warning("MQTT topic ignored: %s", msg.topic)
            return

        response_topic = request["response_topic"]
        service = request["service"]
        command_id = payload.get("command_id")
        command_id_log = command_id if isinstance(command_id, str) else ""
        logger.info(
            "Received MQTT service request topic=%s service=%s command_id=%s response_topic=%s",
            msg.topic,
            service,
            command_id_log,
            response_topic,
        )

        if request.get("ignored"):
            if self.strict_device_uuid:
                logger.info(
                    "Ignoring MQTT service request for device %s; this device is %s",
                    request.get("device_uuid") or "",
                    self.device_uuid,
                )
                return
            logger.warning(
                "Accepting MQTT service request for device %s even though configured device UUID is %s. "
                "Set QBOX_MQTT_STRICT_DEVICE_UUID=true to reject mismatches.",
                request.get("device_uuid") or "",
                self.device_uuid,
            )

        if response_topic is None:
            logger.warning("MQTT topic ignored: %s", msg.topic)
            return

        if command_id and self._is_duplicate_command(command_id):
            logger.info("Ignoring duplicate MQTT command_id=%s service=%s", command_id, service)
            return

        if self._command_handler is None:
            return

        try:
            with self._handler_lock:
                response = self._command_handler(payload, msg.topic)
            if response is None:
                return

            if service in {"wifi.connect", "wifi_connect"}:
                self.ensure_connected(timeout_seconds=20.0)
            if msg.topic.startswith("hardware_agent/request/"):
                self.publish(response_topic, response)
            else:
                published = self.publish(
                    response_topic,
                    {
                        "command_id": command_id,
                        "service": service,
                        "result": response,
                    },
                )
                logger.info(
                    "MQTT service response %s for service=%s command_id=%s response_topic=%s",
                    "published" if published else "queued",
                    service,
                    command_id_log,
                    response_topic,
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

                    notify_fallback = False
                    with self._connection_lock:
                        if self._ble_fallback_handler and not self._fallback_notified:
                            self._fallback_notified = True
                            notify_fallback = True
                    if notify_fallback:
                        try:
                            self._ble_fallback_handler()
                        except Exception:
                            logger.exception("MQTT fallback handler failed")

            self._watchdog_wake.wait(timeout=5)
            self._watchdog_wake.clear()

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
            self._mark_disconnected()
            self._watchdog_wake.set()
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

    @staticmethod
    def _decode_payload(topic: str, payload_bytes: bytes) -> dict[str, Any]:
        raw_payload = payload_bytes.decode().strip()
        if not raw_payload:
            return {}
        try:
            decoded = json.loads(raw_payload)
        except Exception:
            logger.exception("MQTT payload decode failed for topic %s", topic)
            return {}
        if isinstance(decoded, dict):
            return decoded
        return {"value": decoded}

    @staticmethod
    def _response_topic_for_request(topic: str, payload: dict[str, Any]) -> str | None:
        metadata = MqttClient._request_metadata_static(topic, payload, device_uuid=None)
        if metadata is None or metadata.get("ignored"):
            return None
        return str(metadata["response_topic"])

    def _request_metadata(self, topic: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        return self._request_metadata_static(topic, payload, device_uuid=self.device_uuid)

    @staticmethod
    def _request_metadata_static(
        topic: str,
        payload: dict[str, Any],
        *,
        device_uuid: str | None,
    ) -> dict[str, Any] | None:
        if topic.startswith("hardware_agent/request/"):
            service = topic.rsplit("/", 1)[-1].strip()
            if service == "wifi_scan":
                service = "wifi.scan"
            return {
                "service": service,
                "response_topic": topic.replace("/request/", "/response/", 1),
                "device_uuid": "",
                "ignored": False,
            }

        topic_parts = topic.split("/")
        if len(topic_parts) != 5:
            return None
        if topic_parts[0] != "devices" or topic_parts[2] != "services" or topic_parts[4] != "request":
            return None

        requested_device_uuid = topic_parts[1].strip()
        requested_service = topic_parts[3].strip()
        ignored = bool(device_uuid and requested_device_uuid != device_uuid)
        return {
            "service": requested_service,
            "response_topic": f"devices/{requested_device_uuid}/services/{requested_service}/response",
            "device_uuid": requested_device_uuid,
            "ignored": ignored,
        }

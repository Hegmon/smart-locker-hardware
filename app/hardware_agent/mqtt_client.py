from __future__ import annotations

import json
import threading
from typing import Any, Callable

from app.core.mqtt_manager import MQTTManager, get_shared_mqtt_manager
from app.utils.logger import get_logger


logger = get_logger(__name__)


class _Message:
    def __init__(self, topic: str, payload: bytes):
        self.topic = topic
        self.payload = payload


class MqttClient:
    """Compatibility adapter for the hardware agent.

    MQTT connection ownership lives in app.core.mqtt_manager.MQTTManager. This
    class keeps the hardware command routing behavior while sharing the single
    process-wide paho client.
    """

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
        mqtt_manager: MQTTManager | None = None,
    ):
        self.manager = mqtt_manager or get_shared_mqtt_manager()
        self.host = self.manager.config.host
        self.port = self.manager.config.port
        self.keepalive = self.manager.config.keepalive
        self.client_id = f"qbox_{client_id}"
        self.device_uuid = str(device_uuid or self.manager.device_id or client_id).strip()
        self.strict_device_uuid = strict_device_uuid

        self._handler_lock = threading.RLock()
        self._processed_lock = threading.Lock()
        self._subscription_lock = threading.Lock()
        self._processed_commands: list[str] = []
        self._processed_command_set: set[str] = set()
        self._command_handler: Callable[[dict[str, Any], str], dict[str, Any] | None] | None = None
        self._ble_fallback_handler: Callable[[], None] | None = None
        self._fallback_notified = False
        self._subscriptions_registered = False

        self.manager.add_connect_listener(self._on_manager_connect)
        self.manager.add_disconnect_listener(self._on_manager_disconnect)

    def connect(self):
        self._register_subscriptions()
        self.manager.start()

    def disconnect(self):
        self.manager.stop()

    def is_connected(self) -> bool:
        return self.manager.is_connected()

    def ensure_connected(self, timeout_seconds: float = 5.0, *, force_reconnect: bool = False) -> bool:
        return self.manager.ensure_connected(timeout_seconds=timeout_seconds, force_reconnect=force_reconnect)

    def wait_until_connected(self, timeout_seconds: float = 5.0) -> bool:
        return self.manager.wait_until_connected(timeout_seconds=timeout_seconds)

    def publish(self, topic: str, payload: dict[str, Any]) -> bool:
        return self.manager.publish_json(topic, payload, qos=1)

    def register_command_handler(self, handler: Callable[[dict[str, Any], str], dict[str, Any] | None]):
        self._command_handler = handler

    def register_ble_fallback_handler(self, handler: Callable[[], None]):
        self._ble_fallback_handler = handler

    def _register_subscriptions(self) -> None:
        with self._subscription_lock:
            if self._subscriptions_registered:
                return
            if self.device_uuid:
                self.manager.subscribe(f"devices/{self.device_uuid}/services/+/request", self._handle_message, qos=1)
                self.manager.subscribe(f"devices/{self.device_uuid}/commands", self._handle_message, qos=1)
            self.manager.subscribe("devices/+/services/+/request", self._handle_message, qos=1)
            self.manager.subscribe("devices/+/commands", self._handle_message, qos=1)
            self.manager.subscribe("hardware_agent/request/+", self._handle_message, qos=1)
            self._subscriptions_registered = True

    def _handle_message(self, topic: str, payload: bytes) -> None:
        self._on_message(None, None, _Message(topic, payload))

    def _on_connect(self, client, userdata, flags, rc):
        """Compatibility callback for tests and older direct-paho integrations."""
        if rc != 0:
            logger.warning("Hardware MQTT adapter direct connect failed rc=%s", rc)
            return
        if self.device_uuid:
            client.subscribe(f"devices/{self.device_uuid}/services/+/request", qos=1)
            client.subscribe(f"devices/{self.device_uuid}/commands", qos=1)
        client.subscribe("devices/+/services/+/request", qos=1)
        client.subscribe("devices/+/commands", qos=1)
        client.subscribe("hardware_agent/request/+", qos=1)
        self._on_manager_connect()

    def _on_manager_connect(self) -> None:
        self._fallback_notified = False
        logger.info("Hardware MQTT adapter connected through shared manager")

    def _on_manager_disconnect(self) -> None:
        if not self._ble_fallback_handler or self._fallback_notified:
            return
        self._fallback_notified = True
        try:
            self._ble_fallback_handler()
        except Exception:
            logger.exception("MQTT fallback handler failed")

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

            if msg.topic.startswith("hardware_agent/request/"):
                self._publish_service_response(response_topic, response, service=service)
            else:
                published = self._publish_service_response(
                    response_topic,
                    {
                        "command_id": command_id,
                        "service": service,
                        "result": response,
                    },
                    service=service,
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

    def _publish_service_response(self, topic: str, payload: dict[str, Any], *, service: str) -> bool:
        if service in {"wifi.connect", "wifi_connect"} and not self.is_connected():
            self.ensure_connected(timeout_seconds=5.0)

        published = self.publish(topic, payload)
        if published or service not in {"wifi.connect", "wifi_connect"}:
            return published

        if self.ensure_connected(timeout_seconds=3.0, force_reconnect=True):
            published = self.publish(topic, payload)
        return published

    @staticmethod
    def _decode_payload(topic: str, payload_bytes: bytes) -> dict[str, Any]:
        raw_payload = payload_bytes.decode("utf-8", errors="replace").strip()
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
        if len(topic_parts) == 3 and topic_parts[0] == "devices" and topic_parts[2] == "commands":
            requested_device_uuid = topic_parts[1].strip()
            service = str(payload.get("service") or payload.get("command") or "").strip()
            ignored = bool(device_uuid and requested_device_uuid != device_uuid)
            return {
                "service": service,
                "response_topic": f"devices/{requested_device_uuid}/commands/result",
                "device_uuid": requested_device_uuid,
                "ignored": ignored,
            }

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

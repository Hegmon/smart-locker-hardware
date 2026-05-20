from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import paho.mqtt.client as mqtt

from app.utils.logger import get_logger


logger = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DEFAULT_MQTT_HOST = os.getenv("MQTT_HOST", "69.62.125.223")
DEFAULT_MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
DEFAULT_MQTT_KEEPALIVE = int(os.getenv("MQTT_KEEPALIVE", "60"))
DEFAULT_MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
DEFAULT_MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

MessageHandler = Callable[[str, bytes], None]
ConnectionHandler = Callable[[], None]


@dataclass(frozen=True)
class MQTTConfig:
    device_id: str
    host: str
    port: int = 1883
    username: str = ""
    password: str = ""
    keepalive: int = 60


def load_mqtt_config(config_path: Path | None = None) -> MQTTConfig:
    path = config_path or CONFIG_DIR / "backend_device.json"
    if not path.exists():
        raise FileNotFoundError(f"MQTT backend config not found: {path}")

    with path.open("r", encoding="utf-8") as config_file:
        raw_config = json.load(config_file)

    mqtt_config = raw_config.get("mqtt") or {}
    device_id = str(raw_config.get("device_id") or "").strip()
    if not device_id:
        raise ValueError(f"Missing device_id in {path}")

    host = str(mqtt_config.get("host") or DEFAULT_MQTT_HOST).strip()
    if not host:
        raise ValueError(f"Missing mqtt.host in {path}")

    return MQTTConfig(
        device_id=device_id,
        host=host,
        port=int(mqtt_config.get("port") or DEFAULT_MQTT_PORT),
        username=str(mqtt_config.get("username", DEFAULT_MQTT_USERNAME) or ""),
        password=str(mqtt_config.get("password", DEFAULT_MQTT_PASSWORD) or ""),
        keepalive=int(mqtt_config.get("keepalive") or DEFAULT_MQTT_KEEPALIVE),
    )


class MQTTManager:
    """Centralized, process-wide MQTT connection manager."""

    def __init__(
        self,
        config: MQTTConfig,
        *,
        reconnect_interval: float = 5.0,
        max_pending_messages: int = 500,
    ):
        self.config = config
        self.device_id = config.device_id
        self.reconnect_interval = max(1.0, reconnect_interval)
        self.max_pending_messages = max(1, max_pending_messages)

        self.status_topic = f"devices/{self.device_id}/status"
        self.client_id = f"smart-locker-{self.device_id}"
        self.client = self._create_client(self.client_id)
        if config.username:
            self.client.username_pw_set(config.username, config.password or None)
        self.client.will_set(self.status_topic, payload="offline", qos=1, retain=True)
        try:
            self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        except Exception:
            logger.debug("MQTT reconnect delay setup skipped", exc_info=True)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        self._running = False
        self._loop_started = False
        self._connected = False
        self._connected_event = threading.Event()
        self._state_lock = threading.RLock()
        self._publish_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._subscriptions_lock = threading.RLock()
        self._listener_lock = threading.RLock()
        self._reconnect_wake = threading.Event()
        self._reconnect_thread: threading.Thread | None = None

        self._subscriptions: list[tuple[str, int, MessageHandler]] = []
        self._connect_listeners: list[ConnectionHandler] = []
        self._disconnect_listeners: list[ConnectionHandler] = []
        self._pending_messages: OrderedDict[str, tuple[str, int, bool]] = OrderedDict()

    @staticmethod
    def _create_client(client_id: str):
        try:
            return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id, clean_session=True)
        except (AttributeError, TypeError):
            return mqtt.Client(client_id=client_id, clean_session=True)

    def start(self) -> None:
        with self._state_lock:
            if self._running:
                return
            self._running = True
            if not self._loop_started:
                self.client.loop_start()
                self._loop_started = True

        logger.info("Starting shared MQTT manager for %s:%s", self.config.host, self.config.port)
        self._connect_async()
        self._reconnect_thread = threading.Thread(target=self._reconnect_loop, daemon=True, name="mqtt-reconnect")
        self._reconnect_thread.start()

    def stop(self, *, publish_offline: bool = True) -> None:
        with self._state_lock:
            if not self._running:
                return
            self._running = False

        self._reconnect_wake.set()
        if publish_offline and self.is_connected():
            try:
                info = self.client.publish(self.status_topic, "offline", qos=1, retain=True)
                info.wait_for_publish(2.0)
            except Exception:
                logger.debug("Graceful MQTT offline status publish failed", exc_info=True)

        try:
            self.client.disconnect()
        except Exception:
            logger.debug("MQTT disconnect failed during shutdown", exc_info=True)

        if self._reconnect_thread:
            self._reconnect_thread.join(timeout=2.0)
            self._reconnect_thread = None

        if self._loop_started:
            self.client.loop_stop()
            self._loop_started = False

        with self._state_lock:
            self._connected = False
            self._connected_event.clear()
        logger.info("Shared MQTT manager stopped")

    def is_connected(self) -> bool:
        with self._state_lock:
            return self._connected

    def wait_until_connected(self, timeout_seconds: float = 5.0) -> bool:
        return self._connected_event.wait(timeout=max(0.0, timeout_seconds)) or self.is_connected()

    def ensure_connected(self, timeout_seconds: float = 5.0, *, force_reconnect: bool = False) -> bool:
        if self.is_connected() and not force_reconnect:
            return True
        if force_reconnect:
            try:
                self.client.disconnect()
            except Exception:
                logger.debug("MQTT force disconnect failed", exc_info=True)
        self._connect_async()
        self._reconnect_wake.set()
        return self.wait_until_connected(timeout_seconds)

    def publish(
        self,
        topic: str,
        payload: Any,
        *,
        qos: int = 1,
        retain: bool = False,
        queue: bool = True,
    ) -> bool:
        message = self.dumps(payload)
        if not self.is_connected():
            if queue:
                self._queue_publish(topic, message, qos, retain)
            return False

        try:
            with self._publish_lock:
                result = self.client.publish(topic, message, qos=qos, retain=retain)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                return True
            logger.warning("MQTT publish returned rc=%s for topic %s", result.rc, topic)
        except Exception:
            logger.exception("MQTT publish failed for topic %s", topic)

        if queue:
            self._queue_publish(topic, message, qos, retain)
        return False

    def publish_json(self, topic: str, payload: dict[str, Any], *, qos: int = 1, retain: bool = False) -> bool:
        return self.publish(topic, payload, qos=qos, retain=retain)

    def subscribe(self, topic: str, handler: MessageHandler, *, qos: int = 1) -> None:
        with self._subscriptions_lock:
            self._subscriptions.append((topic, qos, handler))
        if self.is_connected():
            try:
                self.client.subscribe(topic, qos=qos)
            except Exception:
                logger.exception("MQTT subscribe failed for topic %s", topic)

    def add_connect_listener(self, handler: ConnectionHandler) -> None:
        with self._listener_lock:
            self._connect_listeners.append(handler)

    def add_disconnect_listener(self, handler: ConnectionHandler) -> None:
        with self._listener_lock:
            self._disconnect_listeners.append(handler)

    @staticmethod
    def dumps(payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, separators=(",", ":"), default=str)

    @staticmethod
    def loads(payload: bytes) -> dict[str, Any]:
        raw_payload = payload.decode("utf-8", errors="replace").strip()
        if not raw_payload:
            return {}
        try:
            decoded = json.loads(raw_payload)
        except Exception:
            return {"value": raw_payload}
        if isinstance(decoded, dict):
            return decoded
        return {"value": decoded}

    def _connect_async(self) -> None:
        try:
            self.client.connect_async(self.config.host, self.config.port, keepalive=self.config.keepalive)
        except Exception:
            logger.exception("Failed to schedule MQTT connection to %s:%s", self.config.host, self.config.port)

    def _reconnect_loop(self) -> None:
        while self._running:
            if not self.is_connected():
                try:
                    self.client.reconnect()
                except Exception:
                    logger.debug("MQTT reconnect attempt failed", exc_info=True)
                    self._connect_async()
            self._reconnect_wake.wait(timeout=self.reconnect_interval)
            self._reconnect_wake.clear()

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            logger.warning("MQTT connection failed with rc=%s", rc)
            return

        with self._state_lock:
            self._connected = True
            self._connected_event.set()

        logger.info("MQTT connected to %s:%s as %s", self.config.host, self.config.port, self.client_id)
        self.publish(self.status_topic, "online", qos=1, retain=True, queue=False)
        self._resubscribe()
        self._flush_pending_messages()
        self._notify_connect()

    def _on_disconnect(self, client, userdata, rc):
        was_connected = self.is_connected()
        with self._state_lock:
            self._connected = False
            self._connected_event.clear()
        if was_connected or self._running:
            logger.warning("MQTT disconnected with rc=%s", rc)
        self._notify_disconnect()
        self._reconnect_wake.set()

    def _on_message(self, client, userdata, msg):
        handlers: list[MessageHandler] = []
        with self._subscriptions_lock:
            for topic_filter, _, handler in self._subscriptions:
                try:
                    if mqtt.topic_matches_sub(topic_filter, msg.topic):
                        handlers.append(handler)
                except Exception:
                    logger.debug("MQTT topic match failed for filter %s", topic_filter, exc_info=True)

        for handler in handlers:
            try:
                handler(msg.topic, msg.payload)
            except Exception:
                logger.exception("MQTT message handler failed for topic %s", msg.topic)

    def _resubscribe(self) -> None:
        with self._subscriptions_lock:
            subscriptions = [(topic, qos) for topic, qos, _ in self._subscriptions]
        for topic, qos in subscriptions:
            try:
                self.client.subscribe(topic, qos=qos)
            except Exception:
                logger.exception("MQTT resubscribe failed for topic %s", topic)

    def _queue_publish(self, topic: str, message: str, qos: int, retain: bool) -> None:
        with self._pending_lock:
            if topic in self._pending_messages:
                self._pending_messages.pop(topic)
            self._pending_messages[topic] = (message, qos, retain)
            while len(self._pending_messages) > self.max_pending_messages:
                self._pending_messages.popitem(last=False)
        logger.info("Queued MQTT publish while disconnected: %s", topic)

    def _flush_pending_messages(self) -> None:
        while self.is_connected():
            with self._pending_lock:
                if not self._pending_messages:
                    return
                topic, (message, qos, retain) = self._pending_messages.popitem(last=False)
            if not self.publish(topic, message, qos=qos, retain=retain, queue=False):
                self._queue_publish(topic, message, qos, retain)
                return

    def _notify_connect(self) -> None:
        with self._listener_lock:
            listeners = list(self._connect_listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:
                logger.exception("MQTT connect listener failed")

    def _notify_disconnect(self) -> None:
        with self._listener_lock:
            listeners = list(self._disconnect_listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:
                logger.exception("MQTT disconnect listener failed")


_shared_manager_lock = threading.Lock()
_shared_manager: MQTTManager | None = None


def get_shared_mqtt_manager(config_path: Path | None = None) -> MQTTManager:
    global _shared_manager
    with _shared_manager_lock:
        if _shared_manager is None:
            _shared_manager = MQTTManager(load_mqtt_config(config_path))
        return _shared_manager

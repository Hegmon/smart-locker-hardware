from __future__ import annotations

import json
import time
import threading
from typing import Callable, Any, Dict

import paho.mqtt.client as mqtt


class MqttClient:
    def __init__(
        self,
        host: str,
        port: int,
        device_id: str,
        keepalive: int = 60,
    ):
        self.host = host
        self.port = port
        self.device_id = device_id
        self.client_id = f"qbox-{device_id}"
        self.keepalive = keepalive

        self._connected = False
        self._running = True

        # local offline queue (important for production)
        self._offline_queue: list[tuple[str, dict]] = []

        # MQTT client
        self.client = mqtt.Client(
            client_id=self.client_id,
            clean_session=True,
        )

        # reliability tuning
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.max_inflight_messages_set(100)
        self.client.max_queued_messages_set(1000)

        # callbacks
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        # handlers
        self._handlers: Dict[str, Callable[[dict[str, Any]], None]] = {}

        # topics
        self.command_topic = f"devices/{device_id}/command"
        self.scan_topic = f"devices/{device_id}/wifi/scan"
        self.state_topic = f"devices/{device_id}/wifi/state"
        self.result_topic = f"devices/{device_id}/command/result"

    # ---------------- CONNECT ----------------
    def connect(self):
        print(f"[MQTT] Connecting to {self.host}:{self.port}...")

        self.client.connect_async(self.host, self.port, self.keepalive)
        self.client.loop_start()

        threading.Thread(target=self._flush_offline_queue, daemon=True).start()

    # ---------------- OFFLINE QUEUE ----------------
    def _flush_offline_queue(self):
        while self._running:
            if self._connected and self._offline_queue:
                print(f"[MQTT] Flushing {len(self._offline_queue)} queued messages")

                for topic, payload in list(self._offline_queue):
                    self._publish_internal(topic, payload)
                    self._offline_queue.remove((topic, payload))

            time.sleep(5)

    # ---------------- CALLBACKS ----------------
    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            print("[MQTT] Connected")

            client.subscribe(self.command_topic, qos=1)
            print(f"[MQTT] Subscribed: {self.command_topic}")
        else:
            print(f"[MQTT] Failed connect rc={rc}")

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        print(f"[MQTT] Disconnected rc={rc}")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())

            handler = self._handlers.get("command")
            if handler:
                handler(payload)

        except Exception as e:
            print(f"[MQTT] Message error: {e}")

    # ---------------- HANDLER ----------------
    def register_command_handler(self, handler: Callable[[dict[str, Any]], None]):
        self._handlers["command"] = handler

    # ---------------- PUBLISH ----------------
    def publish(self, topic: str, payload: dict):
        if not self._connected:
            print("[MQTT] Offline → queueing message")
            self._offline_queue.append((topic, payload))
            return

        self._publish_internal(topic, payload)

    def _publish_internal(self, topic: str, payload: dict):
        try:
            result = self.client.publish(
                topic,
                json.dumps(payload),
                qos=1,
                retain=False,
            )

            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                print(f"[MQTT] publish failed rc={result.rc}")

        except Exception as e:
            print(f"[MQTT] publish error: {e}")
            self._offline_queue.append((topic, payload))

    # ---------------- CLEANUP ----------------
    def disconnect(self):
        print("[MQTT] shutting down")

        self._running = False
        self.client.loop_stop()
        self.client.disconnect()
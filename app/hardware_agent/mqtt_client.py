from __future__ import annotations

import json
import time
import threading
from typing import Callable, Any, Optional

import paho.mqtt.client as mqtt


# =========================================================
# MQTT CLIENT (PRODUCTION GRADE)
# =========================================================
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

        self._connected = False
        self._running = True

        # =====================================================
        # EXTERNAL HANDLERS (IMPORTANT FOR CLEAN ARCH)
        # =====================================================
        self._command_handler: Optional[Callable[[dict, str], dict]] = None
        self._ble_fallback_handler: Optional[Callable[[], None]] = None

        # deduplication (MQTT QoS safety)
        self._processed_commands: set[str] = set()

        # mqtt client
        self.client = mqtt.Client(
            client_id=self.client_id,
            clean_session=True
        )

        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    # =========================================================
    # PUBLIC API
    # =========================================================
    def connect(self):
        self.client.connect(self.host, self.port, self.keepalive)
        self.client.loop_start()

        threading.Thread(target=self._watchdog, daemon=True).start()

    def disconnect(self):
        self._running = False
        self.client.loop_stop()
        self.client.disconnect()

    def publish(self, topic: str, payload: dict):
        if not self._connected:
            return

        self.client.publish(topic, json.dumps(payload), qos=1)

    # =========================================================
    # HANDLER REGISTRATION
    # =========================================================
    def register_command_handler(self, handler: Callable[[dict, str], dict]):
        """
        Main business logic handler (WiFi, BLE, etc.)
        """
        self._command_handler = handler

    def register_ble_fallback_handler(self, handler: Callable[[], None]):
        """
        Called when MQTT is down or WiFi is lost
        """
        self._ble_fallback_handler = handler

    # =========================================================
    # MQTT EVENTS
    # =========================================================
    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True

            # service-based subscription (CLEAN ARCH)
            client.subscribe("devices/+/services/+/request", qos=1)

            print("[MQTT] Connected & subscribed")

        else:
            print(f"[MQTT] Connection failed rc={rc}")

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        print("[MQTT] Disconnected")

    # =========================================================
    # MESSAGE ROUTING (IMPORTANT FIX)
    # =========================================================
    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())

            topic_parts = msg.topic.split("/")

            # SAFE parsing
            if len(topic_parts) < 5:
                return

            device_id = topic_parts[1]
            service = topic_parts[3]

            command_id = payload.get("command_id")

            # =====================================================
            # DUPLICATE PROTECTION (VERY IMPORTANT)
            # =====================================================
            if command_id and command_id in self._processed_commands:
                return

            if command_id:
                self._processed_commands.add(command_id)

            # =====================================================
            # BUSINESS LOGIC HANDLER (NO COUPLING)
            # =====================================================
            if self._command_handler:
                response = self._command_handler(payload, msg.topic)

                if not response:
                    return

                response_topic = f"devices/{device_id}/services/{service}/response"

                self.publish(response_topic, {
                    "command_id": command_id,
                    "service": service,
                    "result": response,
                })

        except Exception as e:
            print(f"[MQTT ERROR] {e}")

    # =========================================================
    # WATCHDOG (CRITICAL FOR IoT RELIABILITY)
    # =========================================================
    def _watchdog(self):
        while self._running:
            if not self._connected:
                try:
                    print("[MQTT] Reconnecting...")
                    self.client.reconnect()

                except Exception:
                    # =================================================
                    # BLE FALLBACK TRIGGER (IMPORTANT)
                    # =================================================
                    if self._ble_fallback_handler:
                        print("[MQTT] Switching to BLE fallback")
                        self._ble_fallback_handler()

            time.sleep(5)
from __future__ import annotations

import json
import time
import threading
from typing import Callable, Any
import paho.mqtt.client as mqtt


class MqttClient:
    def __init__(
        self,
        host: str,
        port: int,
        client_id: str,
        keepalive: int = 60,
    ):
        self.host = host
        self.port = port
        self.client_id = f"qbox-{client_id}"
        self.keepalive = keepalive

        self._connected = False
        self._running = True

        self.client = mqtt.Client(
            client_id=self.client_id,
            clean_session=True,
        )

        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        self._handlers: dict[str, Callable[[dict[str, Any], str], None]] = {}

    # ---------------- CONNECT ----------------
    def connect(self):
        print(f"[MQTT] Connecting to {self.host}:{self.port}")

        self.client.connect_async(self.host, self.port, self.keepalive)
        self.client.loop_start()

        threading.Thread(target=self._watchdog, daemon=True).start()

    # ---------------- WATCHDOG ----------------
    def _watchdog(self):
        while self._running:
            if not self._connected:
                try:
                    print("[MQTT] Reconnecting...")
                    self.client.reconnect()
                except Exception:
                    pass
            time.sleep(5)

    # ---------------- CONNECT CALLBACK ----------------
    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            print("[MQTT] Connected")

            # wildcard subscriptions (IMPORTANT FOR SCALING)
            client.subscribe("devices/+/command", qos=1)
            client.subscribe("devices/+/wifi/scan", qos=1)
            client.subscribe("devices/+/wifi/state", qos=1)
            client.subscribe("devices/+/command/result", qos=1)

        else:
            print(f"[MQTT] Connection failed rc={rc}")

    # ---------------- DISCONNECT ----------------
    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        print(f"[MQTT] Disconnected rc={rc}")

    # ---------------- MESSAGE ----------------
    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())

            topic = msg.topic

            # route based on topic type
            if "command" in topic:
                handler = self._handlers.get("command")
                if handler:
                    handler(payload, topic)

        except Exception as e:
            print(f"[MQTT] message error: {e}")

    # ---------------- HANDLER ----------------
    def register_command_handler(self, handler: Callable):
        self._handlers["command"] = handler

    # ---------------- PUBLISH ----------------
    def publish(self, topic: str, payload: dict):
        if not self._connected:
            print(f"[MQTT] Not connected -> skip publish {topic}")
            return

        self.client.publish(
            topic,
            json.dumps(payload),
            qos=1,
            retain=False,
        )

    # ---------------- STOP ----------------
    def disconnect(self):
        self._running = False
        self.client.loop_stop()
        self.client.disconnect()
from __future__ import annotations

import threading
import time
import random
from datetime import datetime, timezone
from typing import Any

from app.hardware_agent.config import load_agent_config, AgentConfig
from app.hardware_agent.mqtt_client import MqttClient
from app.hardware_agent.scanner import WifiScanner
from app.services.wifi_manager import (
    connect_wifi,
    get_connected_wifi_details,
    WifiCommandError,
)


# ---------------- TIME ----------------
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------- AGENT ----------------
class WifiUploadAgent:
    def __init__(self, config: AgentConfig):
        self.config = config

        self.mqtt = MqttClient(
            host=config.mqtt_host,
            port=config.mqtt_port,
            client_id=config.device_id,
            keepalive=config.mqtt_keepalive,
        )

        self.scanner = WifiScanner(config.interface)

        self._running = False
        self._last_scan_hash = None
        self._processed_commands: set[str] = set()  # 🧠 deduplication

        self.mqtt.register_command_handler(self.handle_command)

    # ---------------- START ----------------
    def start(self):
        print("[AGENT] Starting MQTT agent...")

        self.mqtt.connect()
        self._running = True

        # jitter prevents MQTT storm (IMPORTANT at scale)
        scan_delay = random.randint(0, 5)
        heartbeat_delay = random.randint(0, 5)

        threading.Thread(target=self._scan_loop, args=(scan_delay,), daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, args=(heartbeat_delay,), daemon=True).start()

        while True:
            time.sleep(1)

    # ---------------- SCAN LOOP ----------------
    def _scan_loop(self, jitter: int):
        time.sleep(jitter)

        while self._running:
            try:
                self.publish_wifi_scan()
            except Exception as e:
                print(f"[SCAN ERROR] {e}")

            time.sleep(self.config.scan_interval_seconds)

    # ---------------- HEARTBEAT ----------------
    def _heartbeat_loop(self, jitter: int):
        time.sleep(jitter)

        while self._running:
            try:
                self.publish_status()
            except Exception as e:
                print(f"[HEARTBEAT ERROR] {e}")

            time.sleep(self.config.heartbeat_seconds)

    # ---------------- COMMAND HANDLER ----------------
    def handle_command(self, payload: dict[str, Any]):
        command_id = payload.get("command_id")

        # 🔥 DEDUPLICATION (VERY IMPORTANT for MQTT QoS1)
        if command_id in self._processed_commands:
            print(f"[AGENT] Duplicate command ignored: {command_id}")
            return

        self._processed_commands.add(command_id)

        event_type = payload.get("event_type")
        data = payload.get("payload", {})

        print(f"[AGENT] Command received: {event_type}")

        if event_type == "CONNECT_WIFI":
            self._handle_connect_wifi(command_id, data)
        else:
            print(f"[AGENT] Unknown command: {event_type}")

    def _handle_connect_wifi(self, command_id: str, data: dict[str, Any]):
        ssid = data.get("ssid")
        password = data.get("password")

        try:
            result = connect_wifi(ssid, password)

            self.publish_command_result(
                command_id=command_id,
                status="SUCCESS",
                ssid=ssid,
                message="Connected successfully",
                details=result,
            )

        except WifiCommandError as e:
            self.publish_command_result(
                command_id=command_id,
                status="FAILED",
                ssid=ssid,
                message=str(e),
                details=None,
            )

    # ---------------- WIFI SCAN ----------------
    def publish_wifi_scan(self):
        networks = self.scanner.scan()
        connected = get_connected_wifi_details()

        current_hash = hash(tuple((n.ssid, n.rssi) for n in networks))
        if current_hash == self._last_scan_hash:
            return

        self._last_scan_hash = current_hash

        payload = {
            "device_id": self.config.device_id,
            "device_uuid": self.config.device_uuid,
            "timestamp": utc_now(),
            "connected_ssid": connected.get("connected_ssid"),
            "signal_strength": connected.get("signal_strength"),
            "networks": [n.to_payload() for n in networks],
        }

        self.mqtt.publish(self.config.mqtt_scan_topic, payload)

    # ---------------- STATUS ----------------
    def publish_status(self):
        connected = get_connected_wifi_details()

        payload = {
            "device_id": self.config.device_id,
            "device_uuid": self.config.device_uuid,
            "timestamp": utc_now(),
            "status": "ONLINE",
            "connected_ssid": connected.get("connected_ssid"),
            "signal_strength": connected.get("signal_strength"),
            "rssi": connected.get("rssi"),
        }

        self.mqtt.publish(self.config.mqtt_status_topic, payload)

    # ---------------- RESULT ----------------
    def publish_command_result(
        self,
        command_id: str,
        status: str,
        ssid: str,
        message: str,
        details: Any,
    ):
        payload = {
            "command_id": command_id,
            "device_id": self.config.device_id,
            "status": status,
            "ssid": ssid,
            "message": message,
            "details": details,
            "timestamp": utc_now(),
        }

        self.mqtt.publish(self.config.mqtt_result_topic, payload)


# ---------------- ENTRYPOINT ----------------
def main():
    config = load_agent_config()
    agent = WifiUploadAgent(config)
    agent.start()


if __name__ == "__main__":
    main()
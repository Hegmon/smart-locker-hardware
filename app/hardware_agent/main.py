from __future__ import annotations

import threading
import time
import random
from datetime import datetime, timezone
from typing import Any

from app.hardware_agent.config import load_agent_config, AgentConfig
from app.hardware_agent.mqtt_client import MqttClient
from app.hardware_agent.scanner import WifiScanner

from app.hardware_agent.provisioning.ble.server import BLEServer

from app.services.wifi_manager import (
    connect_wifi,
    get_connected_wifi_details,
    start_hotspot,
    reconnect_saved_wifi,
    WifiCommandError,
)


# =========================================================
# TIME
# =========================================================
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# =========================================================
# NETWORK STATE
# =========================================================
class NetworkState:
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    BLE = "BLE_PROVISIONING"
    HOTSPOT = "HOTSPOT"


# =========================================================
# AGENT
# =========================================================
class WifiUploadAgent:

    def __init__(self, config: AgentConfig):
        self.config = config

        self.mqtt = MqttClient(
            host=config.mqtt_host,
            port=config.mqtt_port,  
            client_id=config.device_id,
            keepalive=config.mqtt_keepalive,
            username=config.mqtt_username,
            password=config.mqtt_password,
        )

        self.scanner = WifiScanner(config.interface)

        # ✅ BLE
        self.ble = BLEServer(config.interface)

        self._running = False
        self._processed_commands: set[str] = set()

        self.network_state = NetworkState.DISCONNECTED
        self.last_good_ssid: str | None = None

        self.mqtt.register_command_handler(self.handle_command)

    # =========================================================
    # START
    # =========================================================
    def start(self):
        print("[AGENT] Starting Smart Locker Agent")

        self.mqtt.connect()
        time.sleep(2)

        self._running = True

        threading.Thread(target=self._watchdog_loop, daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        threading.Thread(target=self._scan_loop, daemon=True).start()

        print("[AGENT] Running...")

        while True:
            time.sleep(1)

    # =========================================================
    # WATCHDOG (CORE OF SYSTEM)
    # =========================================================
    def _watchdog_loop(self):
        while self._running:
            try:
                status = get_connected_wifi_details()
                ssid = status.get("connected_ssid")

                if ssid:
                
                    self.last_good_ssid = ssid
                    self.network_state = NetworkState.CONNECTED

                    # stop BLE if running
                    self._stop_ble()

                else:
                    print("[WATCHDOG] No WiFi → recovery triggered")
                    self._recover_connection()

            except Exception as e:
                print(f"[WATCHDOG ERROR] {e}")

            time.sleep(10)

    # =========================================================
    # RECOVERY ENGINE (CRITICAL)
    # =========================================================
    def _recover_connection(self):
        #  Try previous WiFi
        if self.last_good_ssid:
            try:
                print(f"[RECOVERY] Trying previous WiFi: {self.last_good_ssid}")
                reconnect_saved_wifi(self.last_good_ssid)
                return
            except Exception as e:
                print(f"[RECOVERY] Previous WiFi failed: {e}")

        # Start BLE provisioning
        print("[RECOVERY] Starting BLE provisioning")
        self.network_state = NetworkState.BLE
        self._start_ble()

        # Wait some time for user provisioning
        time.sleep(60)

        # Check again
        status = get_connected_wifi_details()
        if status.get("connected_ssid"):
            print("[RECOVERY] Connected via BLE provisioning")
            return

        #  Fallback to Hotspot
        print("[RECOVERY] BLE failed → starting hotspot")
        try:
            hotspot = start_hotspot()
            self.network_state = NetworkState.HOTSPOT
            print(f"[HOTSPOT] {hotspot}")
        except Exception as e:
            print(f"[HOTSPOT ERROR] {e}")

    # =========================================================
    # BLE CONTROL
    # =========================================================
    def _start_ble(self):
        try:
            threading.Thread(target=self.ble.start, daemon=True).start()
        except Exception as e:
            print(f"[BLE ERROR] {e}")

    def _stop_ble(self):
        try:
            self.ble.stop()
        except Exception:
            pass

    # =========================================================
    # SCAN LOOP
    # =========================================================
    def _scan_loop(self):
        time.sleep(random.randint(0, 5))

        while self._running:
            try:
                self.publish_wifi_scan()
            except Exception as e:
                print(f"[SCAN ERROR] {e}")

            time.sleep(self.config.scan_interval_seconds)

    # =========================================================
    # HEARTBEAT
    # =========================================================
    def _heartbeat_loop(self):
        time.sleep(random.randint(0, 5))

        while self._running:
            try:
                self.publish_status()
            except Exception as e:
                print(f"[HEARTBEAT ERROR] {e}")

            time.sleep(self.config.heartbeat_seconds)

    # =========================================================
    # COMMAND HANDLER (MQTT)
    # =========================================================
    def handle_command(self, payload: dict[str, Any], topic: str):
        command_id = payload.get("command_id")

        if not command_id or command_id in self._processed_commands:
            return

        self._processed_commands.add(command_id)

        service = payload.get("service")
        data = payload.get("data", {})

        if service == "wifi.connect":
            self._handle_wifi_connect(command_id, data)

    # =========================================================
    # WIFI CONNECT (REMOTE COMMAND)
    # =========================================================
    def _handle_wifi_connect(self, command_id: str, data: dict[str, Any]):
        ssid = data.get("ssid")
        password = data.get("password")

        previous = get_connected_wifi_details().get("connected_ssid")

        try:
            result = connect_wifi(ssid, password)

            self.last_good_ssid = ssid
            self.network_state = NetworkState.CONNECTED

            self.publish_command_result(
                command_id,
                "SUCCESS",
                ssid,
                "Connected",
                result,
            )
            return

        except WifiCommandError as e:
            print(f"[CONNECT ERROR] {e}")

        # fallback to previous
        if previous and previous != ssid:
            try:
                reconnect_saved_wifi(previous)

                self.publish_command_result(
                    command_id,
                    "SUCCESS",
                    previous,
                    "Fallback to previous WiFi",
                    {"fallback": True},
                )
                return
            except Exception:
                pass

        # final fallback → BLE (not hotspot directly)
        self._start_ble()

        self.publish_command_result(
            command_id,
            "FAILED",
            ssid,
            "Failed → switched to BLE provisioning",
            None,
        )

    # =========================================================
    # WIFI SCAN
    # =========================================================
    def publish_wifi_scan(self):
        networks = self.scanner.scan()
        connected = get_connected_wifi_details()

        payload = {
            "device_id": self.config.device_id,
            "timestamp": utc_now(),
            "state": self.network_state,
            "connected_ssid": connected.get("connected_ssid"),
            "networks": [n.to_payload() for n in networks],
        }

        self.mqtt.publish(self.config.mqtt_scan_topic, payload)

    # =========================================================
    # STATUS
    # =========================================================
    def publish_status(self):
        connected = get_connected_wifi_details()

        payload = {
            "device_id": self.config.device_id,
            "timestamp": utc_now(),
            "state": self.network_state,
            "connected_ssid": connected.get("connected_ssid"),
        }

        self.mqtt.publish(self.config.mqtt_state_topic, payload)

    # =========================================================
    # RESULT
    # =========================================================
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

        self.mqtt.publish(self.config.mqtt_command_result_topic, payload)


# =========================================================
# ENTRYPOINT
# =========================================================
def main():
    config = load_agent_config()
    agent = WifiUploadAgent(config)
    agent.start()


if __name__ == "__main__":
    main()
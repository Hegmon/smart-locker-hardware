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

        self.ble = BLEServer(config.interface)

        self._running = False
        self._processed_commands: set[str] = set()

        self.network_state = NetworkState.DISCONNECTED
        self.last_good_ssid: str | None = None

        self._last_scan_ssids: set[str] = set()
        self._last_connected_ssid: str | None = None
        self._last_state: str | None = None

        # FIX 2: Track whether a recovery is already in progress so the
        # watchdog never launches two overlapping recovery attempts.
        self._recovery_in_progress = False
        self._recovery_lock = threading.Lock()

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

        try:
            self._initial_publish()
        except Exception as e:
            print(f"[INITIAL PUBLISH ERROR] {e}")

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
                    self._stop_ble()

                else:
                    # FIX 2: Only launch recovery if one is not already
                    # running. This keeps the watchdog loop unblocked —
                    # recovery runs in its own background thread.
                    with self._recovery_lock:
                        if not self._recovery_in_progress:
                            self._recovery_in_progress = True
                            print("[WATCHDOG] No WiFi → recovery triggered")
                            threading.Thread(
                                target=self._recover_connection,
                                daemon=True,
                            ).start()
                        else:
                            print("[WATCHDOG] No WiFi — recovery already running, skipping")

                try:
                    self._maybe_publish_status(status)
                except Exception as e:
                    print(f"[WATCHDOG PUBLISH ERROR] {e}")

            except Exception as e:
                print(f"[WATCHDOG ERROR] {e}")

            time.sleep(10)

    # =========================================================
    # RECOVERY ENGINE (CRITICAL)
    # =========================================================
    def _recover_connection(self):
        # FIX 2: Wrap the entire recovery in a try/finally so the flag is
        # always cleared — even if an unexpected exception is raised — and
        # the watchdog can trigger recovery again on the next cycle.
        try:
            # Step 1: try the last known good WiFi
            if self.last_good_ssid:
                try:
                    print(f"[RECOVERY] Trying previous WiFi: {self.last_good_ssid}")
                    reconnect_saved_wifi(self.last_good_ssid)

                    status = get_connected_wifi_details()
                    if status.get("connected_ssid"):
                        print("[RECOVERY] Reconnected to previous WiFi")
                        self.network_state = NetworkState.CONNECTED
                        return
                except Exception as e:
                    print(f"[RECOVERY] Previous WiFi failed: {e}")

            # Step 2: BLE provisioning
            print("[RECOVERY] Starting BLE provisioning")
            self.network_state = NetworkState.BLE
            self._start_ble()

            # FIX 2: Poll every 5 s instead of sleeping the full 60 s in
            # one block. This means successful provisioning is detected
            # within 5 s and the recovery thread exits early.
            for _ in range(12):          # 12 × 5 s = 60 s total budget
                time.sleep(5)
                status = get_connected_wifi_details()
                if status.get("connected_ssid"):
                    print("[RECOVERY] Connected via BLE provisioning")
                    self.network_state = NetworkState.CONNECTED
                    self._stop_ble()
                    return

            # Step 3: fallback to hotspot
            print("[RECOVERY] BLE timed out → starting hotspot")
            self._stop_ble()
            try:
                hotspot = start_hotspot()
                self.network_state = NetworkState.HOTSPOT
                print(f"[HOTSPOT] {hotspot}")
            except Exception as e:
                print(f"[HOTSPOT ERROR] {e}")

        finally:
            # Always release the lock so the watchdog can start a new
            # recovery attempt if the device is still offline.
            with self._recovery_lock:
                self._recovery_in_progress = False

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
                networks = self.scanner.scan()
                connected = get_connected_wifi_details()
                self._maybe_publish_scan(networks, connected)
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

        # final fallback → BLE
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

        try:
            ssids = [n.ssid for n in networks]
        except Exception:
            ssids = []
        print(f"[PUBLISH SCAN] device={self.config.device_id} state={self.network_state} connected={connected.get('connected_ssid')} networks={ssids}")

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
        print(f"[PUBLISH STATUS] device={self.config.device_id} state={self.network_state} connected={connected.get('connected_ssid')}")

        self.mqtt.publish(self.config.mqtt_state_topic, payload)

    # =========================================================
    # CHANGE-DRIVEN PUBLISH HELPERS
    # =========================================================
    def _initial_publish(self) -> None:
        networks = self.scanner.scan()
        connected = get_connected_wifi_details()

        try:
            self._last_scan_ssids = {n.ssid for n in networks}
        except Exception:
            self._last_scan_ssids = set()
        self._last_connected_ssid = connected.get("connected_ssid")
        self._last_state = self.network_state

        try:
            print("[INITIAL PUBLISH] publishing initial scan and status")
            self.publish_wifi_scan()
        except Exception as e:
            print(f"[INITIAL SCAN ERROR] {e}")

        try:
            self.publish_status()
        except Exception as e:
            print(f"[INITIAL STATUS ERROR] {e}")

    def _maybe_publish_scan(self, networks: list[object], connected: dict[str, Any]) -> None:
        try:
            current_ssids = {n.ssid for n in networks}
        except Exception:
            current_ssids = set()
        if current_ssids != self._last_scan_ssids:
            self._last_scan_ssids = current_ssids
            try:
                print(f"[PUBLISH SCAN][CHANGED] device={self.config.device_id} ssids={list(current_ssids)} connected={connected.get('connected_ssid')}")
                payload = {
                    "device_id": self.config.device_id,
                    "timestamp": utc_now(),
                    "state": self.network_state,
                    "connected_ssid": connected.get("connected_ssid"),
                    "networks": [n.to_payload() for n in networks],
                }
                self.mqtt.publish(self.config.mqtt_scan_topic, payload)
            except Exception as e:
                print(f"[PUBLISH SCAN ERROR] {e}")

    def _maybe_publish_status(self, connected: dict[str, Any]) -> None:
        current_connected = connected.get("connected_ssid")
        if current_connected != self._last_connected_ssid or self.network_state != self._last_state:
            self._last_connected_ssid = current_connected
            self._last_state = self.network_state
            try:
                print(f"[PUBLISH STATUS][CHANGED] device={self.config.device_id} state={self.network_state} connected={current_connected}")
                payload = {
                    "device_id": self.config.device_id,
                    "timestamp": utc_now(),
                    "state": self.network_state,
                    "connected_ssid": current_connected,
                }
                self.mqtt.publish(self.config.mqtt_state_topic, payload)
            except Exception as e:
                print(f"[PUBLISH STATUS ERROR] {e}")

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
    # FAST TELEMETRY (5s)
    # =========================================================
    def _fast_telemetry_loop(self) -> None:
        pass


# =========================================================
# ENTRYPOINT
# =========================================================
def main():
    config = load_agent_config()
    agent = WifiUploadAgent(config)
    agent.start()


if __name__ == "__main__":
    main()
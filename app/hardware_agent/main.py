from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.hardware_agent.config import AgentConfig, load_agent_config
from app.hardware_agent.mqtt_client import MqttClient
from app.hardware_agent.provisioning.ble.server import BLEServer
from app.hardware_agent.scanner import WifiScanner
from app.services.wifi_manager import (
    WifiCommandError,
    connect_wifi,
    get_connected_wifi_details,
    reconnect_saved_wifi,
    start_hotspot,
)
from app.utils.logger import get_logger


logger = get_logger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class NetworkState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    BLE_PROVISIONING = "BLE_PROVISIONING"
    HOTSPOT = "HOTSPOT"


@dataclass(frozen=True)
class NetworkSnapshot:
    state: NetworkState
    connected_ssid: str | None
    last_good_ssid: str | None
    ble_active: bool
    recovery_active: bool
    hotspot_active: bool


class WifiUploadAgent:
    RECOVERY_POLL_SECONDS = 5
    RECOVERY_BLE_TIMEOUT_SECONDS = 60
    WATCHDOG_INTERVAL_SECONDS = 10

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
        self.ble = BLEServer(
            config.interface,
            on_wifi_connected=self._handle_ble_wifi_connected,
        )

        self._running = False
        self._state_lock = threading.RLock()
        self._command_lock = threading.Lock()
        self._recovery_gate = threading.Lock()
        self._ble_gate = threading.Lock()

        self._recovery_in_progress = False
        self._ble_active = False
        self._hotspot_active = False

        self.network_state = NetworkState.DISCONNECTED
        self.last_good_ssid: str | None = None
        self._last_connected_ssid: str | None = None
        self._last_scan_ssids: set[str] = set()
        self._last_status_signature: tuple[str, str | None] | None = None

        self.mqtt.register_command_handler(self.handle_command)
        self.mqtt.register_ble_fallback_handler(self._handle_mqtt_reconnect_pressure)

    def start(self):
        logger.info("Starting Smart Locker hardware agent")
        self.mqtt.connect()
        self._running = True

        threading.Thread(target=self._watchdog_loop, daemon=True, name="wifi-watchdog").start()
        threading.Thread(target=self._heartbeat_loop, daemon=True, name="wifi-heartbeat").start()
        threading.Thread(target=self._scan_loop, daemon=True, name="wifi-scan").start()

        self._initial_publish()

        while self._running:
            time.sleep(1)

    def _watchdog_loop(self):
        while self._running:
            try:
                status = get_connected_wifi_details()
                self._handle_wifi_observation(status, source="watchdog")
                self.publish_status(connected=status, force=False)
            except Exception:
                logger.exception("Watchdog loop failed")

            time.sleep(self.WATCHDOG_INTERVAL_SECONDS)

    def _heartbeat_loop(self):
        time.sleep(random.randint(0, 5))

        while self._running:
            try:
                self.publish_status(force=True)
            except Exception:
                logger.exception("Heartbeat publish failed")

            time.sleep(self.config.heartbeat_seconds)

    def _scan_loop(self):
        time.sleep(random.randint(0, 5))

        while self._running:
            try:
                networks = self.scanner.scan()
                connected = get_connected_wifi_details()
                self._maybe_publish_scan(networks, connected)
            except Exception:
                logger.exception("WiFi scan loop failed")

            time.sleep(self.config.scan_interval_seconds)

    def _handle_wifi_observation(self, status: dict[str, Any], source: str):
        connected_ssid = str(status.get("connected_ssid") or "").strip() or None

        if connected_ssid:
            with self._state_lock:
                self.last_good_ssid = connected_ssid
                self._last_connected_ssid = connected_ssid
                self._hotspot_active = False
            self._transition_to(
                NetworkState.CONNECTED,
                reason=f"WiFi connected via {source}",
                connected_ssid=connected_ssid,
            )
            return

        with self._state_lock:
            current_state = self.network_state
            self._last_connected_ssid = None

        if current_state == NetworkState.CONNECTED:
            self._transition_to(NetworkState.DISCONNECTED, reason=f"WiFi lost via {source}")
            self._ensure_recovery_running(reason=f"WiFi lost via {source}")
            return

        if current_state == NetworkState.DISCONNECTED:
            self._ensure_recovery_running(reason=f"WiFi unavailable via {source}")

    def _handle_ble_wifi_connected(self, ssid: str):
        status = get_connected_wifi_details()
        if status.get("connected_ssid"):
            self._handle_wifi_observation(status, source="ble")
            self.publish_status(connected=status, force=False)
            return

        self._transition_to(
            NetworkState.CONNECTED,
            reason=f"BLE provisioned WiFi {ssid}",
            connected_ssid=ssid,
        )
        self.publish_status(
            connected={
                "connected_ssid": ssid,
                "connected": True,
            },
            force=False,
        )

    def _handle_mqtt_reconnect_pressure(self):
        with self._state_lock:
            current_state = self.network_state

        if current_state == NetworkState.DISCONNECTED:
            self._ensure_recovery_running(reason="MQTT reconnect pressure while offline")

    def _ensure_recovery_running(self, reason: str) -> bool:
        with self._recovery_gate:
            if self._recovery_in_progress:
                return False
            self._recovery_in_progress = True

        logger.info("Recovery flow started: %s", reason)
        threading.Thread(
            target=self._recover_connection,
            daemon=True,
            name="wifi-recovery",
        ).start()
        return True

    def _recover_connection(self):
        try:
            if self._is_connected():
                return

            last_good_ssid = self._snapshot().last_good_ssid
            if last_good_ssid:
                self._transition_to(
                    NetworkState.CONNECTING,
                    reason=f"Reconnect saved WiFi {last_good_ssid}",
                )
                try:
                    reconnect_saved_wifi(last_good_ssid)
                    status = get_connected_wifi_details()
                    if status.get("connected_ssid"):
                        self._handle_wifi_observation(status, source="recovery")
                        return
                except Exception as exc:
                    logger.warning("Reconnect to saved WiFi failed for %s: %s", last_good_ssid, exc)

            self._transition_to(
                NetworkState.BLE_PROVISIONING,
                reason="Recovery entering BLE provisioning",
            )

            deadline = time.monotonic() + self.RECOVERY_BLE_TIMEOUT_SECONDS
            while self._running and time.monotonic() < deadline:
                time.sleep(self.RECOVERY_POLL_SECONDS)
                status = get_connected_wifi_details()
                if status.get("connected_ssid"):
                    self._handle_wifi_observation(status, source="recovery-ble")
                    return

            if self._is_connected():
                return

            self._transition_to(NetworkState.HOTSPOT, reason="BLE provisioning timed out")
            try:
                hotspot = start_hotspot()
                with self._state_lock:
                    self._hotspot_active = True
                logger.info("Hotspot started: %s", hotspot.get("ssid"))
            except Exception:
                logger.exception("Failed to start hotspot after BLE timeout")

        finally:
            with self._recovery_gate:
                self._recovery_in_progress = False

    def _transition_to(
        self,
        new_state: NetworkState,
        *,
        reason: str,
        connected_ssid: str | None = None,
    ) -> bool:
        with self._state_lock:
            previous_state = self.network_state
            if previous_state == new_state:
                if new_state == NetworkState.CONNECTED and connected_ssid:
                    self.last_good_ssid = connected_ssid
                    self._last_connected_ssid = connected_ssid
                return False

            self.network_state = new_state
            if new_state == NetworkState.CONNECTED and connected_ssid:
                self.last_good_ssid = connected_ssid
                self._last_connected_ssid = connected_ssid
            if new_state != NetworkState.HOTSPOT:
                self._hotspot_active = False

        logger.info("State transition %s -> %s (%s)", previous_state, new_state, reason)
        self._on_state_enter(new_state)
        return True

    def _on_state_enter(self, state: NetworkState):
        if state == NetworkState.BLE_PROVISIONING:
            self._start_ble()
            return

        if state in {
            NetworkState.CONNECTED,
            NetworkState.CONNECTING,
            NetworkState.DISCONNECTED,
            NetworkState.HOTSPOT,
        }:
            self._stop_ble()

    def _start_ble(self):
        with self._ble_gate:
            if self._ble_active:
                return
            self._ble_active = True

        logger.info("BLE provisioning enabled")
        self.ble.start_async()

    def _stop_ble(self):
        with self._ble_gate:
            if not self._ble_active:
                return
            self._ble_active = False

        logger.info("BLE provisioning disabled")
        self.ble.stop()

    def handle_command(self, payload: dict[str, Any], topic: str) -> dict[str, Any]:
        command_id = payload.get("command_id")
        service = self._extract_service(payload, topic)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

        if service == "wifi.connect":
            return self._handle_wifi_connect(command_id, data)
        if service == "wifi.scan":
            return self._handle_wifi_scan_command()
        if service == "wifi.status":
            return self._build_status_payload(get_connected_wifi_details())

        return {
            "status": "unsupported_service",
            "service": service,
        }

    def _handle_wifi_connect(self, command_id: str | None, data: dict[str, Any]) -> dict[str, Any]:
        ssid = str(data.get("ssid") or "").strip()
        password = str(data.get("password") or "")
        previous = get_connected_wifi_details().get("connected_ssid")

        if not ssid:
            response = {
                "status": "FAILED",
                "message": "ssid is required",
            }
            if command_id:
                self.publish_command_result(command_id, "FAILED", "", "ssid is required", None)
            return response

        with self._command_lock:
            self._transition_to(NetworkState.CONNECTING, reason=f"Remote WiFi connect for {ssid}")

            try:
                result = connect_wifi(ssid, password)
                connected = result.get("connection") if isinstance(result, dict) else {}
                self._handle_wifi_observation(connected or get_connected_wifi_details(), source="mqtt")

                response = {
                    "status": "SUCCESS",
                    "ssid": ssid,
                    "message": "Connected",
                    "details": result,
                }
                if command_id:
                    self.publish_command_result(command_id, "SUCCESS", ssid, "Connected", result)
                return response

            except WifiCommandError as exc:
                logger.warning("WiFi connect failed for %s: %s", ssid, exc)
                last_error = str(exc)
            except Exception as exc:
                logger.exception("Unexpected WiFi connect failure for %s", ssid)
                last_error = str(exc)

            if previous and previous != ssid:
                try:
                    result = reconnect_saved_wifi(previous)
                    connected = result.get("connection") if isinstance(result, dict) else {}
                    self._handle_wifi_observation(connected or get_connected_wifi_details(), source="mqtt-fallback")

                    response = {
                        "status": "SUCCESS",
                        "ssid": previous,
                        "message": "Fallback to previous WiFi",
                        "details": {"fallback": True, "result": result, "error": last_error},
                    }
                    if command_id:
                        self.publish_command_result(
                            command_id,
                            "SUCCESS",
                            previous,
                            "Fallback to previous WiFi",
                            {"fallback": True, "result": result, "error": last_error},
                        )
                    return response
                except Exception as exc:
                    logger.warning("Fallback reconnect failed for %s: %s", previous, exc)

            self._transition_to(
                NetworkState.BLE_PROVISIONING,
                reason=f"Remote WiFi connect failed for {ssid}",
            )
            response = {
                "status": "FAILED",
                "ssid": ssid,
                "message": "Failed -> switched to BLE provisioning",
                "details": {"error": last_error},
            }
            if command_id:
                self.publish_command_result(
                    command_id,
                    "FAILED",
                    ssid,
                    "Failed -> switched to BLE provisioning",
                    {"error": last_error},
                )
            return response

    def _handle_wifi_scan_command(self) -> dict[str, Any]:
        networks = self.scanner.scan()
        connected = get_connected_wifi_details()
        return {
            "device_id": self.config.device_id,
            "timestamp": utc_now(),
            "state": self._snapshot().state,
            "connected_ssid": connected.get("connected_ssid"),
            "networks": [network.to_payload() for network in networks],
        }

    def publish_wifi_scan(self):
        networks = self.scanner.scan()
        connected = get_connected_wifi_details()
        payload = {
            "device_id": self.config.device_id,
            "timestamp": utc_now(),
            "state": self._snapshot().state,
            "connected_ssid": connected.get("connected_ssid"),
            "networks": [network.to_payload() for network in networks],
        }
        self.mqtt.publish(self.config.mqtt_scan_topic, payload)

    def publish_status(self, connected: dict[str, Any] | None = None, force: bool = False):
        connected = connected or get_connected_wifi_details()
        payload = self._build_status_payload(connected)
        signature = (payload["state"], payload.get("connected_ssid"))

        with self._state_lock:
            should_publish = force or signature != self._last_status_signature
            if should_publish:
                self._last_status_signature = signature

        if should_publish:
            self.mqtt.publish(self.config.mqtt_state_topic, payload)

    def _initial_publish(self):
        try:
            self.publish_wifi_scan()
        except Exception:
            logger.exception("Initial WiFi scan publish failed")

        try:
            self.publish_status(force=True)
        except Exception:
            logger.exception("Initial status publish failed")

    def _maybe_publish_scan(self, networks: list[object], connected: dict[str, Any]):
        current_ssids = {network.ssid for network in networks}

        with self._state_lock:
            if current_ssids == self._last_scan_ssids:
                return
            self._last_scan_ssids = current_ssids

        payload = {
            "device_id": self.config.device_id,
            "timestamp": utc_now(),
            "state": self._snapshot().state,
            "connected_ssid": connected.get("connected_ssid"),
            "networks": [network.to_payload() for network in networks],
        }
        self.mqtt.publish(self.config.mqtt_scan_topic, payload)

    def _build_status_payload(self, connected: dict[str, Any]) -> dict[str, Any]:
        snapshot = self._snapshot()
        return {
            "device_id": self.config.device_id,
            "timestamp": utc_now(),
            "state": snapshot.state,
            "connected_ssid": connected.get("connected_ssid"),
        }

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

    def _extract_service(self, payload: dict[str, Any], topic: str) -> str:
        service = payload.get("service")
        if isinstance(service, str) and service.strip():
            return service.strip()

        topic_parts = topic.split("/")
        if len(topic_parts) >= 5:
            return topic_parts[3]
        return ""

    def _is_connected(self) -> bool:
        return bool(get_connected_wifi_details().get("connected_ssid"))

    def _snapshot(self) -> NetworkSnapshot:
        with self._state_lock:
            return NetworkSnapshot(
                state=self.network_state,
                connected_ssid=self._last_connected_ssid,
                last_good_ssid=self.last_good_ssid,
                ble_active=self._ble_active,
                recovery_active=self._recovery_in_progress,
                hotspot_active=self._hotspot_active,
            )


def main():
    config = load_agent_config()
    agent = WifiUploadAgent(config)
    agent.start()


if __name__ == "__main__":
    main()

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.deployment.bootstrap import bootstrap_device
from app.deployment.health_server import AgentHealthServer
from app.deployment.runtime_config import get_int_setting
from app.deployment.validation import validate_runtime_configuration
from app.hardware_agent.config import AgentConfig, load_agent_config
from app.hardware_agent.mqtt_client import MqttClient
from app.hardware_agent.provisioning.ble.server import BLEServer
from app.services.hardware_manager import initialize_gpio_with_retry
from app.hardware_agent.scanner import WifiScanner
from app.services.wifi_manager import (
    WifiCommandError,
    connect_wifi,
    get_connected_wifi_details,
    list_saved_wifi_networks,
    reconnect_saved_wifi,
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
    WATCHDOG_INTERVAL_SECONDS = 10
    WIFI_PRIORITY_RECHECK_SECONDS = 30
    BLE_RESTART_SECONDS = 15

    def __init__(self, config: AgentConfig):
        self.config = config
        self.health_server = AgentHealthServer(
            "0.0.0.0",
            get_int_setting("HARDWARE_AGENT_HEALTH_PORT", 8091),
            self._health_payload,
        )
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
        self._last_scan_connected_ssid: str | None = None
        self._last_status_signature: tuple[str, str | None] | None = None
        self._last_priority_reconnect_at = 0.0

        self.mqtt.register_command_handler(self.handle_command)
        self.mqtt.register_ble_fallback_handler(self._handle_mqtt_reconnect_pressure)

    def start(self):
        logger.info("Starting Smart Locker hardware agent")
        bootstrap_device()
        validate_runtime_configuration()
        gpio_init = initialize_gpio_with_retry()
        if not gpio_init.get("initialized"):
            logger.warning("GPIO initialization deferred: %s", gpio_init)
        self.health_server.start()
        self.mqtt.connect()
        self.mqtt.wait_until_connected(timeout_seconds=3.0)
        self._running = True
        initial_status = get_connected_wifi_details()
        self._handle_wifi_observation(initial_status, source="startup")
        if initial_status.get("connected_ssid"):
            self.publish_status(connected=initial_status, force=True)

        threading.Thread(target=self._watchdog_loop, daemon=True, name="wifi-watchdog").start()
        threading.Thread(target=self._heartbeat_loop, daemon=True, name="wifi-heartbeat").start()
        threading.Thread(target=self._scan_loop, daemon=True, name="wifi-scan").start()

        self._initial_publish()

        while self._running:
            time.sleep(1)

    def _health_payload(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        return {
            "status": "ok",
            "service": "hardware_agent",
            "device_id": self.config.device_id,
            "mqtt_connected": self.mqtt.is_connected(),
            "network_state": snapshot.state.value,
            "connected_ssid": snapshot.connected_ssid,
            "hotspot_active": snapshot.hotspot_active,
            "ble_active": snapshot.ble_active,
        }

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
                self._maybe_switch_to_best_saved_network(networks, connected)
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
            transitioned = self._transition_to(
                NetworkState.CONNECTED,
                reason=f"WiFi connected via {source}",
                connected_ssid=connected_ssid,
            )
            if not transitioned and (self._ble_active or self.ble.is_bluetooth_enabled() or self.ble.is_advertising()):
                logger.info("WiFi is connected on %s, forcing BLE shutdown", connected_ssid)
                self._stop_ble()
            if transitioned:
                self._publish_connectivity_snapshot(status)
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

        transitioned = self._transition_to(
            NetworkState.CONNECTED,
            reason=f"BLE provisioned WiFi {ssid}",
            connected_ssid=ssid,
        )
        if transitioned:
            self._publish_connectivity_snapshot(
                {
                    "connected_ssid": ssid,
                    "connected": True,
                }
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

            if self._attempt_best_saved_reconnect(source="recovery", allow_roam=False):
                return

            self._transition_to(
                NetworkState.BLE_PROVISIONING,
                reason="Recovery entering BLE provisioning",
            )

            next_ble_restart_at = time.monotonic() + self.BLE_RESTART_SECONDS
            while self._running:
                time.sleep(self.RECOVERY_POLL_SECONDS)
                status = get_connected_wifi_details()
                if status.get("connected_ssid"):
                    self._handle_wifi_observation(status, source="recovery-ble")
                    return

                now = time.monotonic()
                if now >= next_ble_restart_at:
                    if self.ble.startup_failed() or not self.ble.is_running():
                        logger.info("BLE provisioning is not active, retrying BLE startup")
                        self._restart_ble_provisioning()
                    next_ble_restart_at = now + self.BLE_RESTART_SECONDS

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
        if state in {
            NetworkState.DISCONNECTED,
            NetworkState.BLE_PROVISIONING,
        }:
            self._start_ble()
            return

        if state in {
            NetworkState.CONNECTED,
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

    def _restart_ble_provisioning(self):
        with self._ble_gate:
            self._ble_active = False
        self._start_ble()

    def handle_command(self, payload: dict[str, Any], topic: str) -> dict[str, Any]:
        command_id = payload.get("command_id")
        service = self._extract_service(payload, topic)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

        if service in {"wifi.connect", "wifi_connect"}:
            return self._handle_wifi_connect(command_id, data)
        if service in {"wifi.scan", "wifi_scan"}:
            return self._handle_wifi_scan_command()
        if service in {"wifi.status", "state"}:
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

            current_status = get_connected_wifi_details()
            if current_status.get("connected_ssid") == ssid:
                self._handle_wifi_observation(current_status, source="mqtt-late-success")
                response = {
                    "status": "SUCCESS",
                    "ssid": ssid,
                    "message": "Connected",
                    "details": {"late_success": True, "error": last_error},
                }
                if command_id:
                    self.publish_command_result(
                        command_id,
                        "SUCCESS",
                        ssid,
                        "Connected",
                        {"late_success": True, "error": last_error},
                    )
                return response

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
        return self._build_wifi_scan_payload(networks, connected)

    def publish_wifi_scan(self):
        self._publish_wifi_scan_payload(self.scanner.scan(), get_connected_wifi_details())

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
        current_connected_ssid = connected.get("connected_ssid")

        with self._state_lock:
            if (
                current_ssids == self._last_scan_ssids
                and current_connected_ssid == self._last_scan_connected_ssid
            ):
                return
            self._last_scan_ssids = current_ssids
            self._last_scan_connected_ssid = current_connected_ssid

        self._publish_wifi_scan_payload(networks, connected)

    def _build_status_payload(self, connected: dict[str, Any]) -> dict[str, Any]:
        snapshot = self._snapshot()
        return {
            "device_id": self.config.device_id,
            "timestamp": utc_now(),
            "state": snapshot.state.value,
            "wifi_connected": bool(connected.get("connected_ssid")),
            "ssid": connected.get("connected_ssid") or "",
            "connected_ssid": connected.get("connected_ssid") or "",
            "ip": connected.get("ip_address") or "",
            "signal_strength": connected.get("signal_strength", 0),
            "rssi": connected.get("rssi", 0),
            "bluetooth_enabled": self.ble.is_bluetooth_enabled(),
            "ble_advertising": self.ble.is_advertising(),
            "mqtt_connected": self.mqtt.is_connected(),
            "recovery_active": snapshot.recovery_active,
            "hotspot_active": snapshot.hotspot_active,
        }

    def _publish_connectivity_snapshot(self, connected: dict[str, Any]):
        self.publish_status(connected=connected, force=True)
        try:
            networks = self.scanner.scan()
        except Exception:
            logger.exception("WiFi scan failed during connect snapshot publish")
            return

        with self._state_lock:
            self._last_scan_ssids = {network.ssid for network in networks}
            self._last_scan_connected_ssid = connected.get("connected_ssid")
        self._publish_wifi_scan_payload(networks, connected)

    def _publish_wifi_scan_payload(self, networks: list[object], connected: dict[str, Any]):
        payload = self._build_wifi_scan_payload(networks, connected)
        self.mqtt.publish(self.config.mqtt_scan_topic, payload)

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

        if topic.startswith("hardware_agent/request/"):
            return topic.rsplit("/", 1)[-1].strip()

        topic_parts = topic.split("/")
        if len(topic_parts) >= 5:
            return topic_parts[3]
        return ""

    def _build_wifi_scan_payload(self, networks: list[object], connected: dict[str, Any]) -> dict[str, Any]:
        connected_ssid = connected.get("connected_ssid")
        return {
            "device_id": self.config.device_id,
            "timestamp": utc_now(),
            "state": self._snapshot().state.value,
            "connected_ssid": connected_ssid or "",
            "networks": [
                {
                    "ssid": network.ssid,
                    "rssi": network.rssi,
                    "security": getattr(network, "security", "UNKNOWN"),
                    "connected": network.ssid == connected_ssid,
                }
                for network in networks
            ],
        }

    def _maybe_switch_to_best_saved_network(
        self,
        networks: list[object],
        connected: dict[str, Any],
    ) -> None:
        if not self._running:
            return

        with self._state_lock:
            current_state = self.network_state
            recovery_in_progress = self._recovery_in_progress

        if current_state == NetworkState.HOTSPOT or recovery_in_progress:
            return

        current_ssid = str(connected.get("connected_ssid") or "").strip() or None
        current_rssi = self._network_rssi(networks, current_ssid)
        candidate = self._select_best_saved_network(networks)

        if candidate is None:
            return

        threshold = max(1, self.config.signal_change_threshold)
        should_reconnect = False
        reason = ""

        if not current_ssid:
            should_reconnect = True
            reason = "WiFi disconnected"
        elif candidate.ssid != current_ssid:
            current_missing = current_rssi is None
            stronger_signal = current_rssi is None or candidate.rssi >= current_rssi + threshold
            if current_missing or stronger_signal:
                should_reconnect = True
                reason = (
                    f"saved WiFi {candidate.ssid} is stronger than {current_ssid}"
                    if not current_missing
                    else f"current WiFi {current_ssid} is unavailable"
                )

        if not should_reconnect:
            return

        now = time.monotonic()
        with self._state_lock:
            if now - self._last_priority_reconnect_at < self.WIFI_PRIORITY_RECHECK_SECONDS:
                return
            self._last_priority_reconnect_at = now

        self._attempt_saved_wifi_reconnect(candidate.ssid, source="priority", reason=reason)

    def _attempt_best_saved_reconnect(self, source: str, allow_roam: bool) -> bool:
        networks = self.scanner.scan()
        connected = get_connected_wifi_details()
        current_ssid = str(connected.get("connected_ssid") or "").strip() or None
        current_rssi = self._network_rssi(networks, current_ssid)
        candidates = self._build_saved_wifi_candidates(networks)
        if not candidates:
            logger.info("No saved WiFi reconnect candidates are available")
            return False

        if current_ssid and current_rssi is not None:
            strongest = candidates[0]
            logger.info(
                "Current WiFi %s RSSI %s dBm, strongest saved WiFi candidate %s RSSI %s dBm",
                current_ssid,
                current_rssi,
                strongest["ssid"],
                strongest["rssi"],
            )

        for candidate in candidates:
            ssid = candidate["ssid"]
            if not allow_roam and current_ssid and ssid == current_ssid:
                logger.info("Already connected to strongest saved WiFi %s", current_ssid)
                return False
            if self._attempt_saved_wifi_reconnect(
                ssid,
                source=source,
                reason=f"saved WiFi candidate RSSI {candidate['rssi']} dBm",
            ):
                return True

        return False

    def _attempt_saved_wifi_reconnect(self, ssid: str, *, source: str, reason: str) -> bool:
        if not self._command_lock.acquire(blocking=False):
            logger.info("Skipping WiFi reconnect to %s because another network command is active", ssid)
            return False

        try:
            self._transition_to(NetworkState.CONNECTING, reason=f"{source} reconnect to {ssid} ({reason})")
            result = reconnect_saved_wifi(ssid)
            status = result.get("connection") if isinstance(result, dict) else {}
            if not status.get("connected_ssid"):
                status = get_connected_wifi_details()
            if status.get("connected_ssid"):
                logger.info("Selected saved WiFi %s connected successfully", ssid)
                self._handle_wifi_observation(status, source=source)
                return True
            logger.warning("Selected saved WiFi %s did not become active", ssid)
            self._handle_wifi_observation(get_connected_wifi_details(), source=f"{source}-post-failure")
            return False
        except Exception as exc:
            logger.warning("Saved WiFi reconnect failed for %s: %s", ssid, exc)
            self._handle_wifi_observation(get_connected_wifi_details(), source=f"{source}-error")
            return False
        finally:
            self._command_lock.release()

    def _select_best_saved_network(self, networks: list[object]):
        candidates = self._build_saved_wifi_candidates(networks)
        if not candidates:
            return None
        selected_ssid = candidates[0]["ssid"]
        for network in networks:
            if network.ssid == selected_ssid:
                logger.info("Selected strongest saved WiFi: %s (%s dBm)", network.ssid, network.rssi)
                return network
        return None

    def _build_saved_wifi_candidates(self, networks: list[object]) -> list[dict[str, Any]]:
        saved_ssids = set(list_saved_wifi_networks())
        saved_networks = [network for network in networks if network.ssid in saved_ssids]
        visible_saved_ssids = {network.ssid for network in saved_networks}

        logger.info(
            "Visible saved WiFi networks: %s",
            [
                {
                    "ssid": network.ssid,
                    "rssi": network.rssi,
                    "security": getattr(network, "security", "UNKNOWN"),
                }
                for network in saved_networks
            ] or "none",
        )

        candidates = [
            {
                "ssid": network.ssid,
                "rssi": network.rssi,
            }
            for network in sorted(saved_networks, key=lambda network: network.rssi, reverse=True)
        ]

        for ssid in saved_ssids:
            if ssid not in visible_saved_ssids:
                candidates.append({"ssid": ssid, "rssi": -999})

        logger.info("Saved WiFi reconnect candidates: %s", candidates)
        return candidates

    @staticmethod
    def _network_rssi(networks: list[object], ssid: str | None) -> int | None:
        if not ssid:
            return None
        for network in networks:
            if network.ssid == ssid:
                return network.rssi
        return None

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

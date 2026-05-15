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
from app.deployment.runtime_config import get_bool_setting, get_int_setting
from app.deployment.validation import validate_runtime_configuration
from app.hardware_agent.config import AgentConfig, load_agent_config
from app.hardware_agent.connectivity import ConnectivityConfig, InternetConnectivityChecker
from app.hardware_agent.mqtt_client import MqttClient
from app.hardware_agent.provisioning.ble.server import BLEServer
from app.hardware_agent.reconnect_policy import ReconnectPolicy, ReconnectPolicyConfig
from app.hardware_agent.saved_networks import SavedNetworkManager
from app.hardware_agent.wifi_responses import build_wifi_connect_failure, build_wifi_connect_success
from app.services.hardware_manager import initialize_gpio_with_retry
from app.hardware_agent.scanner import WifiScanner
from app.services.wifi_manager import (
    DEFAULT_HOTSPOT_CONNECTION,
    DEFAULT_HOTSPOT_SSID,
    DEFAULT_WIFI_REMOTE_CONNECT_ACTIVATION_SECONDS,
    DEFAULT_WIFI_REMOTE_CONNECT_WAIT_SECONDS,
    WifiCommandError,
    connect_wifi,
    get_connected_wifi_details,
    reconnect_saved_wifi,
)
from app.utils.logger import get_logger


logger = get_logger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class NetworkState(str, Enum):
    BOOTING = "BOOTING"
    CHECKING_INTERNET = "CHECKING_INTERNET"
    DISCONNECTED = "DISCONNECTED"
    SCANNING_SAVED_NETWORKS = "SCANNING_SAVED_NETWORKS"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    BLE_PROVISIONING = "BLE_PROVISIONING"
    RECONNECTING = "RECONNECTING"
    ERROR_BACKOFF = "ERROR_BACKOFF"
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
    WATCHDOG_INTERVAL_SECONDS = 10
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
            device_uuid=config.device_uuid,
            strict_device_uuid=get_bool_setting("QBOX_MQTT_STRICT_DEVICE_UUID", False),
            keepalive=config.mqtt_keepalive,
            username=config.mqtt_username,
            password=config.mqtt_password,
        )
        self.scanner = WifiScanner(config.interface)
        self.internet = InternetConnectivityChecker(
            ConnectivityConfig(
                method=config.connectivity_check_method,
                timeout_seconds=config.connectivity_check_timeout_seconds,
                retries=config.connectivity_check_retries,
                dns_host=config.connectivity_dns_host,
                ping_host=config.connectivity_ping_host,
                http_url=config.connectivity_http_url,
            )
        )
        self.saved_networks = SavedNetworkManager(
            config.state_file,
            retry_base_delay_seconds=config.retry_base_delay_seconds,
            max_retry_delay_seconds=config.max_retry_delay_seconds,
        )
        self.policy = ReconnectPolicy(
            ReconnectPolicyConfig(
                minimum_signal_dbm=config.min_signal_dbm,
                switch_hysteresis_dbm=config.switch_hysteresis_dbm,
                switch_cooldown_seconds=config.switch_cooldown_seconds,
            )
        )
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

        self.network_state = NetworkState.BOOTING
        self.last_good_ssid: str | None = None
        self._last_connected_ssid: str | None = None
        self._last_scan_ssids: set[str] = set()
        self._last_scan_connected_ssid: str | None = None
        self._last_status_signature: tuple[str, str | None] | None = None
        self._last_priority_reconnect_at = 0.0
        self._last_saved_retry_at = 0.0
        self._ble_started_at = 0.0
        self._last_internet_check_at = 0.0
        self._last_internet_online = False
        self._manual_connect_active = False
        self._manual_connect_ssid: str | None = None
        self._auto_reconnect_deferred = False
        self._post_connect_roam_hold_until = 0.0
        self._post_connect_roam_hold_ssid: str | None = None

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
        initial_status = self._startup_network_flow()
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

    def _startup_network_flow(self) -> dict[str, Any]:
        self._transition_to(NetworkState.CHECKING_INTERNET, reason="startup internet check")
        status = get_connected_wifi_details()
        if status.get("connected_ssid") and self._internet_is_available(force=True):
            self._handle_wifi_observation(status, source="startup-online")
            return status

        logger.info("Startup internet unavailable; scanning saved WiFi before BLE provisioning")
        if self._attempt_best_saved_reconnect(source="startup", allow_roam=True):
            return get_connected_wifi_details()

        if self._should_pause_automatic_wifi():
            logger.info("Startup WiFi recovery paused because a remote WiFi command is active")
            self._transition_to(NetworkState.DISCONNECTED, reason="startup paused for remote WiFi command")
            return get_connected_wifi_details()

        self._transition_to(
            NetworkState.BLE_PROVISIONING,
            reason="startup requires provisioning; no saved network restored internet",
        )
        self._ensure_recovery_running(reason="startup saved WiFi unavailable; BLE provisioning active")
        return get_connected_wifi_details()

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
        connection_profile = str(status.get("connection_profile") or "").strip()

        if self._is_setup_hotspot(status):
            logger.info(
                "WiFi is associated to setup hotspot %s via %s; keeping BLE provisioning active",
                connected_ssid or connection_profile or DEFAULT_HOTSPOT_SSID,
                source,
            )
            with self._state_lock:
                self._hotspot_active = True
                self._last_connected_ssid = None
            if not self._ble_active:
                self._transition_to(NetworkState.BLE_PROVISIONING, reason=f"setup hotspot active via {source}")
            return

        if connected_ssid:
            with self._state_lock:
                current_state_before_online = self.network_state
                previous_connected_ssid = self._last_connected_ssid
                manual_connect_active = self._manual_connect_active
                manual_connect_ssid = self._manual_connect_ssid
            if not self._internet_is_available(force=source in {"startup-online", "ble", "mqtt", "recovery", "startup"}):
                with self._state_lock:
                    current_state = self.network_state
                    recovery_in_progress = self._recovery_in_progress
                if (
                    source == "watchdog"
                    and manual_connect_active
                    and manual_connect_ssid
                    and connected_ssid == manual_connect_ssid
                ):
                    logger.info(
                        "WiFi associated to %s via watchdog while remote connect is active; deferring recovery until command validation finishes",
                        connected_ssid,
                    )
                    with self._state_lock:
                        self._last_connected_ssid = connected_ssid
                    return
                logger.warning(
                    "WiFi associated to %s via %s but internet validation failed",
                    connected_ssid,
                    source,
                )
                with self._state_lock:
                    self._last_connected_ssid = connected_ssid
                if (
                    recovery_in_progress
                    or source.startswith("recovery")
                    or current_state == NetworkState.BLE_PROVISIONING
                ):
                    self._transition_to(
                        NetworkState.BLE_PROVISIONING,
                        reason=f"internet unavailable via {source}; keeping BLE provisioning active",
                    )
                else:
                    self._transition_to(NetworkState.DISCONNECTED, reason=f"internet unavailable via {source}")
                    self._ensure_recovery_running(reason=f"internet unavailable via {source}")
                return

            if (
                source == "watchdog"
                and manual_connect_active
                and manual_connect_ssid
                and connected_ssid == manual_connect_ssid
            ):
                logger.info(
                    "WiFi %s is online via watchdog while remote connect is active; deferring MQTT refresh to command handler",
                    connected_ssid,
                )
                with self._state_lock:
                    self._last_connected_ssid = connected_ssid
                return

            with self._state_lock:
                self.last_good_ssid = connected_ssid
                self._last_connected_ssid = connected_ssid
                self._hotspot_active = False
            force_mqtt_refresh = (
                previous_connected_ssid != connected_ssid
                or current_state_before_online
                in {
                    NetworkState.DISCONNECTED,
                    NetworkState.RECONNECTING,
                    NetworkState.CONNECTING,
                    NetworkState.SCANNING_SAVED_NETWORKS,
                    NetworkState.BLE_PROVISIONING,
                }
                or source in {
                    "priority",
                    "recovery",
                    "recovery-retry",
                    "saved-retry",
                    "mqtt",
                    "mqtt-late-success",
                }
            )
            if manual_connect_active and source.startswith("mqtt"):
                logger.info(
                    "Deferring MQTT reconnect for %s via %s to remote WiFi response publisher",
                    connected_ssid,
                    source,
                )
            else:
                self._ensure_mqtt_connected_after_wifi_online(
                    source=source,
                    ssid=connected_ssid,
                    force_refresh=force_mqtt_refresh,
                )
            self.saved_networks.mark_success(connected_ssid)
            transitioned = self._transition_to(
                NetworkState.CONNECTED,
                reason=f"WiFi connected via {source}",
                connected_ssid=connected_ssid,
            )
            if not transitioned and (self._ble_active or self.ble.is_bluetooth_enabled() or self.ble.is_advertising()):
                logger.info("WiFi is connected on %s, forcing BLE shutdown", connected_ssid)
                self._stop_ble()
            if transitioned:
                if manual_connect_active and source == "mqtt":
                    logger.info("Deferring connectivity snapshot publish until remote WiFi connect response is ready")
                else:
                    self._publish_connectivity_snapshot(status)
            return

        with self._state_lock:
            current_state = self.network_state
            self._last_connected_ssid = None

        if current_state == NetworkState.CONNECTED:
            self._transition_to(NetworkState.DISCONNECTED, reason=f"WiFi lost via {source}")
            self._ensure_recovery_running(reason=f"WiFi lost via {source}")
            return

        if current_state in {NetworkState.DISCONNECTED, NetworkState.ERROR_BACKOFF}:
            self._ensure_recovery_running(reason=f"WiFi unavailable via {source}")

    def _handle_ble_wifi_connected(self, ssid: str) -> bool:
        status = get_connected_wifi_details()
        if status.get("connected_ssid") and self._internet_is_available(force=True):
            self._handle_wifi_observation(status, source="ble")
            self._activate_post_connect_roam_hold(ssid, source="BLE provisioning")
            self._schedule_best_network_check(reason=f"post BLE connect {ssid}")
            self.publish_status(connected=status, force=False)
            return True

        logger.warning("BLE provisioned WiFi %s did not pass internet validation", ssid)
        self.saved_networks.mark_failure(ssid, "internet validation failed after BLE provisioning")
        self._ensure_recovery_running(reason=f"BLE provisioned {ssid} without internet")
        return False

    def _handle_mqtt_reconnect_pressure(self):
        with self._state_lock:
            current_state = self.network_state

        if current_state == NetworkState.DISCONNECTED and not self._should_pause_automatic_wifi():
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
            if self._should_pause_automatic_wifi():
                logger.info("Recovery paused because a remote WiFi command is active")
                return

            self._transition_to(NetworkState.SCANNING_SAVED_NETWORKS, reason="recovery scanning saved WiFi")
            if self._attempt_best_saved_reconnect(source="recovery", allow_roam=False):
                return
            if self._should_pause_automatic_wifi():
                logger.info("Recovery will not enter BLE while a remote WiFi command is active")
                return
            self._transition_to(
                NetworkState.BLE_PROVISIONING,
                reason="saved WiFi unavailable; recovery entering BLE provisioning",
            )

            next_ble_restart_at = time.monotonic() + self.BLE_RESTART_SECONDS
            next_saved_retry_at = time.monotonic() + self.config.reconnect_interval_seconds
            while self._running:
                time.sleep(5)
                status = get_connected_wifi_details()
                if status.get("connected_ssid"):
                    self._handle_wifi_observation(status, source="recovery-ble")
                    if self._internet_is_available():
                        return

                now = time.monotonic()
                if now >= next_saved_retry_at:
                    if self._should_pause_automatic_wifi():
                        next_saved_retry_at = now + self.config.reconnect_interval_seconds
                        continue
                    if self._attempt_best_saved_reconnect(source="recovery-retry", allow_roam=True):
                        return
                    self._transition_to(
                        NetworkState.BLE_PROVISIONING,
                        reason="saved WiFi retry unavailable; keeping BLE provisioning active",
                    )
                    next_saved_retry_at = now + self.config.reconnect_interval_seconds

                if (
                    self._ble_active
                    and self.config.ble_discoverable_timeout_seconds > 0
                    and self._ble_started_at > 0
                    and now - self._ble_started_at >= self.config.ble_discoverable_timeout_seconds
                ):
                    logger.info("BLE provisioning discoverable timeout expired")
                    self._stop_ble()
                    if not self.config.ble_reenable_after_timeout:
                        self._transition_to(NetworkState.ERROR_BACKOFF, reason="BLE timeout; waiting for saved WiFi retry")
                        next_ble_restart_at = now + self.config.internet_check_interval_seconds
                    else:
                        next_ble_restart_at = now

                if now >= next_ble_restart_at:
                    if self.config.ble_reenable_after_timeout and (self.ble.startup_failed() or not self.ble.is_running()):
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
        restart_ble = False
        same_state = False
        with self._state_lock:
            previous_state = self.network_state
            if previous_state == new_state:
                same_state = True
                if new_state == NetworkState.CONNECTED and connected_ssid:
                    self.last_good_ssid = connected_ssid
                    self._last_connected_ssid = connected_ssid
                if (
                    new_state == NetworkState.BLE_PROVISIONING
                    and (not self._ble_active or not self.ble.is_running())
                ):
                    restart_ble = True
                if restart_ble:
                    logger.info("BLE provisioning state is active but server is not running; restarting BLE")
            else:
                self.network_state = new_state
                if new_state == NetworkState.CONNECTED and connected_ssid:
                    self.last_good_ssid = connected_ssid
                    self._last_connected_ssid = connected_ssid
                if new_state != NetworkState.HOTSPOT:
                    self._hotspot_active = False

        if same_state:
            if restart_ble:
                self._restart_ble_provisioning()
            return False

        logger.info("State transition %s -> %s (%s)", previous_state, new_state, reason)
        self._on_state_enter(new_state)
        return True

    def _on_state_enter(self, state: NetworkState):
        if state == NetworkState.BLE_PROVISIONING:
            self._start_ble()
            return

        if state in {
            NetworkState.BOOTING,
            NetworkState.CHECKING_INTERNET,
            NetworkState.DISCONNECTED,
            NetworkState.CONNECTING,
            NetworkState.RECONNECTING,
            NetworkState.CONNECTED,
            NetworkState.HOTSPOT,
            NetworkState.ERROR_BACKOFF,
        }:
            self._stop_ble()

    def _start_ble(self):
        with self._ble_gate:
            if self._ble_active:
                return
            self._ble_active = True

        logger.info("BLE provisioning enabled")
        with self._state_lock:
            self._ble_started_at = time.monotonic()
        self.ble.start_async()

    def _stop_ble(self):
        with self._ble_gate:
            if not self._ble_active:
                return
            self._ble_active = False

        logger.info("BLE provisioning disabled")
        with self._state_lock:
            self._ble_started_at = 0.0
        self.ble.stop()

    def _restart_ble_provisioning(self):
        with self._ble_gate:
            self._ble_active = False
        self._start_ble()

    def handle_command(self, payload: dict[str, Any], topic: str) -> dict[str, Any]:
        command_id = payload.get("command_id")
        service = self._extract_service(payload, topic)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        command_id_log = command_id if isinstance(command_id, str) else ""
        logger.info("Handling MQTT service request service=%s command_id=%s", service, command_id_log)

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
                "ssid": "",
                "message": "ssid is required",
                "details": {"reason": "missing_ssid"},
            }
            if command_id:
                self.publish_command_result(command_id, "FAILED", "", "ssid is required", response["details"])
            return response

        with self._state_lock:
            self._manual_connect_active = True
            self._manual_connect_ssid = ssid
            self._auto_reconnect_deferred = False

        try:
            with self._command_lock:
                self._transition_to(NetworkState.CONNECTING, reason=f"Remote WiFi connect for {ssid}")
                try:
                    result = connect_wifi(
                        ssid,
                        password,
                        activation_timeout=DEFAULT_WIFI_REMOTE_CONNECT_ACTIVATION_SECONDS,
                        connection_wait_timeout=DEFAULT_WIFI_REMOTE_CONNECT_WAIT_SECONDS,
                    )
                    connected = result.get("connection") if isinstance(result, dict) else {}
                    connected = connected or get_connected_wifi_details()
                    if not connected.get("connected_ssid") or not self._internet_is_available(force=True):
                        raise WifiCommandError(f"Connected to {ssid} but internet validation failed")
                    self._handle_wifi_observation(connected, source="mqtt")
                    self._activate_post_connect_roam_hold(ssid, source="MQTT wifi.connect")
                    self._schedule_best_network_check(reason=f"post remote connect {ssid}")

                    response = build_wifi_connect_success(ssid, connected)
                    if command_id:
                        self.publish_command_result(command_id, "SUCCESS", ssid, "Connected", response["details"])
                    logger.info("WiFi connect service request succeeded for ssid=%s command_id=%s", ssid, command_id or "")
                    return response

                except WifiCommandError as exc:
                    logger.warning("WiFi connect failed for %s: %s", ssid, exc)
                    last_error = str(exc)
                    self.saved_networks.mark_failure(ssid, last_error)
                except Exception as exc:
                    logger.exception("Unexpected WiFi connect failure for %s", ssid)
                    last_error = str(exc)
                    self.saved_networks.mark_failure(ssid, last_error)

                current_status = get_connected_wifi_details()
                if current_status.get("connected_ssid") == ssid and self._internet_is_available(force=True):
                    self._handle_wifi_observation(current_status, source="mqtt-late-success")
                    self._activate_post_connect_roam_hold(ssid, source="MQTT wifi.connect late success")
                    self._schedule_best_network_check(reason=f"post late remote connect {ssid}")
                    response = build_wifi_connect_success(ssid, current_status)
                    if command_id:
                        self.publish_command_result(
                            command_id,
                            "SUCCESS",
                            ssid,
                            "Connected",
                            response["details"],
                        )
                    logger.info("WiFi connect service request succeeded late for ssid=%s command_id=%s", ssid, command_id or "")
                    return response

                fallback_ssid = ""
                if previous and previous != ssid:
                    try:
                        result = reconnect_saved_wifi(previous)
                        connected = result.get("connection") if isinstance(result, dict) else {}
                        connected = connected or get_connected_wifi_details()
                        if not connected.get("connected_ssid") or not self._internet_is_available(force=True):
                            raise WifiCommandError(f"Fallback to {previous} failed internet validation")
                        self._handle_wifi_observation(connected, source="mqtt-fallback")
                        fallback_ssid = previous
                    except Exception as exc:
                        logger.warning("Fallback reconnect failed for %s: %s", previous, exc)

                if self._has_saved_wifi_profiles():
                    self._transition_to(
                        NetworkState.ERROR_BACKOFF,
                        reason=f"Remote WiFi connect failed for {ssid}; saved profiles exist",
                    )
                else:
                    self._transition_to(
                        NetworkState.BLE_PROVISIONING,
                        reason=f"Remote WiFi connect failed for {ssid}",
                    )
                response = build_wifi_connect_failure(ssid, last_error, fallback_ssid=fallback_ssid)
                if command_id:
                    self.publish_command_result(
                        command_id,
                        "FAILED",
                        ssid,
                        response["message"],
                        response["details"],
                    )
                logger.info(
                    "WiFi connect service request failed for ssid=%s command_id=%s reason=%s",
                    ssid,
                    command_id or "",
                    response["details"].get("reason"),
                )
                return response
        finally:
            with self._state_lock:
                self._manual_connect_active = False
                self._manual_connect_ssid = None
                self._auto_reconnect_deferred = False

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

    def _ensure_mqtt_connected_after_wifi_online(
        self,
        *,
        source: str,
        ssid: str,
        force_refresh: bool = False,
    ) -> None:
        if self.mqtt.is_connected() and not force_refresh:
            return
        logger.info(
            "WiFi %s is online via %s; ensuring MQTT is %s",
            ssid,
            source,
            "refreshed" if force_refresh else "connected",
        )
        if self.mqtt.ensure_connected(timeout_seconds=20.0, force_reconnect=force_refresh):
            logger.info("MQTT ready after WiFi online via %s", source)
        else:
            logger.warning("MQTT still disconnected after WiFi online via %s; publishes will be queued", source)

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
        with self._state_lock:
            manual_connect_active = self._manual_connect_active
        if status == "SUCCESS" and ssid and not manual_connect_active:
            self._ensure_mqtt_connected_after_wifi_online(source="command-result", ssid=ssid)
        self.mqtt.publish(self.config.mqtt_command_result_topic, payload)

    def _extract_service(self, payload: dict[str, Any], topic: str) -> str:
        topic_parts = topic.split("/")
        if len(topic_parts) == 5 and topic_parts[0] == "devices" and topic_parts[2] == "services":
            return topic_parts[3].strip()

        service = payload.get("service")
        if isinstance(service, str) and service.strip():
            return service.strip()

        if topic.startswith("hardware_agent/request/"):
            return topic.rsplit("/", 1)[-1].strip()

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
        if self._should_pause_automatic_wifi():
            logger.info("Skipping saved WiFi switch because a remote WiFi command is active")
            return

        current_ssid = str(connected.get("connected_ssid") or "").strip() or None
        if self._post_connect_roam_hold_active(current_ssid):
            return

        current_rssi = self._network_rssi(networks, current_ssid)
        policy_candidate = self._select_best_saved_candidate(networks)
        should_reconnect, reason = self.policy.should_switch(
            current_ssid=current_ssid,
            current_rssi=current_rssi,
            candidate=policy_candidate,
            last_switch_at=self._last_priority_reconnect_at,
        )
        if not should_reconnect or policy_candidate is None:
            logger.info("Skipping saved WiFi switch: %s", reason)
            return

        now = time.monotonic()
        with self._state_lock:
            self._last_priority_reconnect_at = now

        self._attempt_saved_wifi_reconnect(policy_candidate.ssid, source="priority", reason=reason)

    def _attempt_best_saved_reconnect(self, source: str, allow_roam: bool) -> bool:
        if self._should_pause_automatic_wifi():
            logger.info("Saved WiFi reconnect paused for %s because a remote WiFi command is active", source)
            with self._state_lock:
                self._auto_reconnect_deferred = True
            return False

        now = time.monotonic()
        with self._state_lock:
            if source not in {"startup", "recovery"} and now - self._last_saved_retry_at < self.config.reconnect_interval_seconds:
                logger.info("Saved WiFi reconnect throttled for %s", source)
                return False
            self._last_saved_retry_at = now
            current_state = self.network_state
            keep_ble_active = current_state == NetworkState.BLE_PROVISIONING and source.startswith("recovery")

        networks = self.scanner.scan()
        connected = get_connected_wifi_details()
        current_ssid = str(connected.get("connected_ssid") or "").strip() or None
        current_rssi = self._network_rssi(networks, current_ssid)
        if keep_ble_active:
            logger.info("Scanning saved WiFi in background while BLE provisioning remains active")
        else:
            self._transition_to(NetworkState.SCANNING_SAVED_NETWORKS, reason=f"{source} scan saved WiFi")
        candidates = self._build_saved_wifi_candidates(networks)
        if not candidates:
            logger.info("No saved WiFi reconnect candidates are available")
            return False

        if keep_ble_active:
            logger.info("Saved WiFi candidates are available, but BLE provisioning is active; waiting for app command")
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
            with self._state_lock:
                self._auto_reconnect_deferred = True
            return False

        try:
            self._transition_to(NetworkState.RECONNECTING, reason=f"{source} reconnect to {ssid} ({reason})")
            result = reconnect_saved_wifi(ssid)
            status = result.get("connection") if isinstance(result, dict) else {}
            if not status.get("connected_ssid"):
                status = get_connected_wifi_details()
            if status.get("connected_ssid") and self._internet_is_available(force=True):
                logger.info("Selected saved WiFi %s connected successfully and passed internet check", ssid)
                self._ensure_mqtt_connected_after_wifi_online(
                    source=source,
                    ssid=ssid,
                    force_refresh=source in {"priority", "recovery", "recovery-retry", "saved-retry"},
                )
                self.saved_networks.mark_success(ssid)
                self._handle_wifi_observation(status, source=source)
                return True
            logger.warning("Selected saved WiFi %s did not become active or internet-valid", ssid)
            self.saved_networks.mark_failure(ssid, "internet validation failed after reconnect")
            self._handle_wifi_observation(get_connected_wifi_details(), source=f"{source}-post-failure")
            return False
        except Exception as exc:
            logger.warning("Saved WiFi reconnect failed for %s: %s", ssid, exc)
            self.saved_networks.mark_failure(ssid, str(exc))
            self._handle_wifi_observation(get_connected_wifi_details(), source=f"{source}-error")
            return False
        finally:
            self._command_lock.release()

    def _retry_saved_networks_without_ble(self) -> None:
        while self._running:
            time.sleep(self.config.reconnect_interval_seconds)
            if self._should_pause_automatic_wifi():
                continue
            if self._is_connected():
                return
            if not self._has_saved_wifi_profiles():
                self._transition_to(
                    NetworkState.BLE_PROVISIONING,
                    reason="no saved WiFi profiles remain; provisioning required",
                )
                return
            self._attempt_best_saved_reconnect(source="saved-retry", allow_roam=True)

    def _schedule_best_network_check(self, *, reason: str) -> None:
        def _run_check() -> None:
            delay_seconds = max(3, self.config.post_connect_roam_hold_seconds)
            time.sleep(delay_seconds)
            try:
                networks = self.scanner.scan()
                connected = get_connected_wifi_details()
                self._maybe_switch_to_best_saved_network(networks, connected)
            except Exception:
                logger.exception("Post-connect best saved WiFi check failed")

        logger.info(
            "Scheduling best saved WiFi check in %s seconds: %s",
            max(3, self.config.post_connect_roam_hold_seconds),
            reason,
        )
        threading.Thread(target=_run_check, daemon=True, name="wifi-post-connect-roam").start()

    def _activate_post_connect_roam_hold(self, ssid: str, *, source: str) -> None:
        hold_seconds = max(0, self.config.post_connect_roam_hold_seconds)
        if hold_seconds <= 0:
            return
        deadline = time.monotonic() + hold_seconds
        with self._state_lock:
            self._post_connect_roam_hold_until = deadline
            self._post_connect_roam_hold_ssid = ssid
            self._last_priority_reconnect_at = deadline - self.config.switch_cooldown_seconds
        logger.info(
            "Holding requested WiFi %s for %s seconds after %s before stronger saved WiFi roaming",
            ssid,
            hold_seconds,
            source,
        )

    def _post_connect_roam_hold_active(self, current_ssid: str | None) -> bool:
        now = time.monotonic()
        with self._state_lock:
            hold_until = self._post_connect_roam_hold_until
            hold_ssid = self._post_connect_roam_hold_ssid

        if not hold_until or now >= hold_until:
            return False
        if hold_ssid and current_ssid and hold_ssid != current_ssid:
            return False

        remaining = int(hold_until - now)
        logger.info(
            "Skipping saved WiFi switch because post-connect hold is active for %s seconds on %s",
            remaining,
            hold_ssid or current_ssid or "requested WiFi",
        )
        return True

    def _should_pause_automatic_wifi(self) -> bool:
        with self._state_lock:
            return self._manual_connect_active

    def _has_saved_wifi_profiles(self) -> bool:
        try:
            return bool(self.saved_networks.list())
        except Exception:
            logger.exception("Saved WiFi profile check failed")
            return False

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

    def _select_best_saved_candidate(self, networks: list[object]):
        candidates = self.policy.build_candidates(
            networks,
            self.saved_networks.policy_networks(),
        )
        if not candidates:
            return None
        candidate = candidates[0]
        logger.info(
            "Selected saved WiFi candidate %s RSSI %s dBm priority %s failures %s",
            candidate.ssid,
            candidate.rssi,
            candidate.priority,
            candidate.failure_count,
        )
        return candidate

    def _build_saved_wifi_candidates(self, networks: list[object]) -> list[dict[str, Any]]:
        saved_records = self.saved_networks.list()
        saved_ssids = {record.ssid for record in saved_records}
        policy_candidates = self.policy.build_candidates(
            networks,
            [record.to_policy_network() for record in saved_records],
        )
        visible_saved_ssids = {candidate.ssid for candidate in policy_candidates}

        logger.info(
            "Visible saved WiFi networks above %s dBm: %s",
            self.config.min_signal_dbm,
            [
                {
                    "ssid": candidate.ssid,
                    "rssi": candidate.rssi,
                    "priority": candidate.priority,
                    "failure_count": candidate.failure_count,
                }
                for candidate in policy_candidates
            ] or "none",
        )

        candidates = [
            {
                "ssid": candidate.ssid,
                "rssi": candidate.rssi,
            }
            for candidate in policy_candidates
        ]

        for ssid in saved_ssids:
            if ssid not in visible_saved_ssids:
                logger.info("Saved WiFi %s ignored: not visible, weak, or in backoff", ssid)

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
        status = get_connected_wifi_details()
        if self._is_setup_hotspot(status):
            return False
        return bool(status.get("connected_ssid")) and self._internet_is_available()

    @staticmethod
    def _is_setup_hotspot(status: dict[str, Any]) -> bool:
        connected_ssid = str(status.get("connected_ssid") or "").strip()
        connection_profile = str(status.get("connection_profile") or "").strip()
        return (
            connected_ssid == DEFAULT_HOTSPOT_SSID
            or connection_profile == DEFAULT_HOTSPOT_CONNECTION
        )

    def _internet_is_available(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        with self._state_lock:
            if (
                not force
                and now - self._last_internet_check_at < self.config.internet_check_interval_seconds
            ):
                return self._last_internet_online

        online = self.internet.is_online()
        with self._state_lock:
            self._last_internet_check_at = now
            self._last_internet_online = online
        return online

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

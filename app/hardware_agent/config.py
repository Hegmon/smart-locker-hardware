from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.config import (
    QBOX_WIFI_AGENT_DEVICE_ID,
    WIFI_INTERFACE,
    MQTT_HOST,
    MQTT_PORT,
    MQTT_KEEPALIVE,
    MQTT_USERNAME,
    MQTT_PASSWORD,

    QBOX_WIFI_AGENT_HEARTBEAT_SECONDS,
    QBOX_WIFI_AGENT_SCAN_INTERVAL_SECONDS,
    QBOX_WIFI_AGENT_STATE_HEARTBEAT_SECONDS,
    QBOX_WIFI_AGENT_COMMAND_POLL_INTERVAL_SECONDS,
    QBOX_WIFI_AGENT_SIGNAL_CHANGE_THRESHOLD,
    QBOX_WIFI_AGENT_MIN_SIGNAL_DBM,
    QBOX_WIFI_AGENT_SWITCH_HYSTERESIS_DBM,
    QBOX_WIFI_AGENT_SWITCH_COOLDOWN_SECONDS,
    QBOX_WIFI_AGENT_POST_CONNECT_ROAM_HOLD_SECONDS,
    QBOX_WIFI_AGENT_INTERNET_CHECK_INTERVAL_SECONDS,
    QBOX_WIFI_AGENT_RECONNECT_INTERVAL_SECONDS,
    QBOX_WIFI_AGENT_CONNECTIVITY_CHECK_METHOD,
    QBOX_WIFI_AGENT_CONNECTIVITY_CHECK_TIMEOUT_SECONDS,
    QBOX_WIFI_AGENT_CONNECTIVITY_CHECK_RETRIES,
    QBOX_WIFI_AGENT_CONNECTIVITY_DNS_HOST,
    QBOX_WIFI_AGENT_CONNECTIVITY_PING_HOST,
    QBOX_WIFI_AGENT_CONNECTIVITY_HTTP_URL,
    QBOX_WIFI_AGENT_BLE_DISCOVERABLE_TIMEOUT_SECONDS,
    QBOX_WIFI_AGENT_BLE_REENABLE_AFTER_TIMEOUT,
    QBOX_WIFI_AGENT_WIFI_CONNECT_TIMEOUT_SECONDS,
    QBOX_WIFI_AGENT_RETRY_MAX_ATTEMPTS,
    QBOX_WIFI_AGENT_RETRY_BASE_DELAY_SECONDS,
    QBOX_WIFI_AGENT_MAX_RETRY_DELAY_SECONDS,
    QBOX_WIFI_AGENT_MAX_BATCH_SIZE,
    QBOX_WIFI_AGENT_QUEUE_FILE,
    QBOX_WIFI_AGENT_STATE_FILE,
)

from app.services.backend_state import load_backend_state


# =========================================================
# UTIL
# =========================================================
def _clamp(value: int, minimum: int) -> int:
    return max(value, minimum)


# =========================================================
# MQTT TOPIC BUILDERS (YOUR STANDARDIZED SYSTEM)
# =========================================================
def request(device_id: str, service: str):
    return f"devices/{device_id}/services/{service}/request"


def response(device_id: str, service: str):
    return f"devices/{device_id}/services/{service}/response"


def event(device_id: str, event_type: str):
    return f"devices/{device_id}/events/{event_type}"


# =========================================================
# CONFIG
# =========================================================
@dataclass(frozen=True)
class AgentConfig:
    # -------- DEVICE --------
    device_uuid: str
    device_id: str
    interface: str

    # -------- MQTT --------
    mqtt_host: str
    mqtt_port: int
    mqtt_keepalive: int
    mqtt_username: str
    mqtt_password: str

    mqtt_wifi_request_topic: str
    mqtt_wifi_response_topic: str

    mqtt_event_wifi: str
    mqtt_event_state: str
    mqtt_event_scan: str
    mqtt_request_wifi_scan_topic: str
    mqtt_response_wifi_scan_topic: str
    mqtt_request_state_topic: str
    mqtt_response_state_topic: str
    # compatibility/topic aliases used by WifiUploadAgent
    mqtt_command_topic: str
    mqtt_command_result_topic: str
    mqtt_scan_topic: str
    mqtt_state_topic: str

    # -------- INTERVALS --------
    scan_interval_seconds: int
    heartbeat_seconds: int
    state_heartbeat_seconds: int
    command_poll_interval_seconds: int

    # -------- RETRY / LOGIC --------
    signal_change_threshold: int
    min_signal_dbm: int
    switch_hysteresis_dbm: int
    switch_cooldown_seconds: int
    post_connect_roam_hold_seconds: int
    internet_check_interval_seconds: int
    reconnect_interval_seconds: int
    connectivity_check_method: str
    connectivity_check_timeout_seconds: float
    connectivity_check_retries: int
    connectivity_dns_host: str
    connectivity_ping_host: str
    connectivity_http_url: str
    ble_discoverable_timeout_seconds: int
    ble_reenable_after_timeout: bool
    wifi_connect_timeout_seconds: int
    retry_max_attempts: int
    retry_base_delay_seconds: float
    max_retry_delay_seconds: int
    max_batch_size: int

    # -------- STORAGE --------
    state_file: Path
    queue_file: Path


# =========================================================
# LOAD CONFIG
# =========================================================
def load_agent_config() -> AgentConfig:
    backend_state = load_backend_state()

    device_uuid = (
        str(backend_state.get("device_uuid") or "").strip()
        or QBOX_WIFI_AGENT_DEVICE_ID
    )

    device_id = str(backend_state.get("device_id") or "").strip() or device_uuid

    if not device_uuid:
        raise RuntimeError("Device UUID missing. Register device first.")

    # =====================================================
    # MQTT SERVICE TOPICS (CLEAN ARCHITECTURE)
    # =====================================================
    mqtt_wifi_request_topic = request(device_uuid, "wifi")
    mqtt_wifi_response_topic = response(device_uuid, "wifi")

    # EVENTS (telemetry / monitoring)
    mqtt_event_wifi = event(device_uuid, "wifi")
    mqtt_event_state = event(device_uuid, "state")
    mqtt_event_scan = event(device_uuid, "scan")

    return AgentConfig(
        # -------- DEVICE --------
        device_uuid=device_uuid,
        device_id=device_id,
        interface=WIFI_INTERFACE,

        # -------- MQTT --------
        mqtt_host=MQTT_HOST,
        mqtt_port=MQTT_PORT,
        mqtt_keepalive=MQTT_KEEPALIVE,
        mqtt_username=MQTT_USERNAME,
        mqtt_password=MQTT_PASSWORD,

        mqtt_wifi_request_topic=mqtt_wifi_request_topic,
        mqtt_wifi_response_topic=mqtt_wifi_response_topic,

        mqtt_event_wifi=mqtt_event_wifi,
        mqtt_event_state=mqtt_event_state,
        mqtt_event_scan=mqtt_event_scan,
        mqtt_request_wifi_scan_topic="hardware_agent/request/wifi_scan",
        mqtt_response_wifi_scan_topic="hardware_agent/response/wifi_scan",
        mqtt_request_state_topic="hardware_agent/request/state",
        mqtt_response_state_topic="hardware_agent/response/state",
        # compatibility/topic aliases used by WifiUploadAgent
        mqtt_command_topic=f"devices/{device_uuid}/command",
        mqtt_command_result_topic=f"devices/{device_uuid}/command/result",
        mqtt_scan_topic=mqtt_event_scan,
        mqtt_state_topic=mqtt_event_state,

        # -------- INTERVALS --------
        scan_interval_seconds=_clamp(QBOX_WIFI_AGENT_SCAN_INTERVAL_SECONDS, 15),
        heartbeat_seconds=_clamp(QBOX_WIFI_AGENT_HEARTBEAT_SECONDS, 30),
        state_heartbeat_seconds=_clamp(QBOX_WIFI_AGENT_STATE_HEARTBEAT_SECONDS, 60),
        command_poll_interval_seconds=_clamp(QBOX_WIFI_AGENT_COMMAND_POLL_INTERVAL_SECONDS, 5),

        # -------- LOGIC --------
        signal_change_threshold=_clamp(QBOX_WIFI_AGENT_SIGNAL_CHANGE_THRESHOLD, 1),
        min_signal_dbm=min(-1, QBOX_WIFI_AGENT_MIN_SIGNAL_DBM),
        switch_hysteresis_dbm=_clamp(QBOX_WIFI_AGENT_SWITCH_HYSTERESIS_DBM, 1),
        switch_cooldown_seconds=_clamp(QBOX_WIFI_AGENT_SWITCH_COOLDOWN_SECONDS, 30),
        post_connect_roam_hold_seconds=_clamp(QBOX_WIFI_AGENT_POST_CONNECT_ROAM_HOLD_SECONDS, 300),
        internet_check_interval_seconds=_clamp(QBOX_WIFI_AGENT_INTERNET_CHECK_INTERVAL_SECONDS, 10),
        reconnect_interval_seconds=_clamp(QBOX_WIFI_AGENT_RECONNECT_INTERVAL_SECONDS, 10),
        connectivity_check_method=QBOX_WIFI_AGENT_CONNECTIVITY_CHECK_METHOD,
        connectivity_check_timeout_seconds=max(QBOX_WIFI_AGENT_CONNECTIVITY_CHECK_TIMEOUT_SECONDS, 1.0),
        connectivity_check_retries=_clamp(QBOX_WIFI_AGENT_CONNECTIVITY_CHECK_RETRIES, 1),
        connectivity_dns_host=QBOX_WIFI_AGENT_CONNECTIVITY_DNS_HOST,
        connectivity_ping_host=QBOX_WIFI_AGENT_CONNECTIVITY_PING_HOST,
        connectivity_http_url=QBOX_WIFI_AGENT_CONNECTIVITY_HTTP_URL,
        ble_discoverable_timeout_seconds=_clamp(QBOX_WIFI_AGENT_BLE_DISCOVERABLE_TIMEOUT_SECONDS, 30),
        ble_reenable_after_timeout=QBOX_WIFI_AGENT_BLE_REENABLE_AFTER_TIMEOUT,
        wifi_connect_timeout_seconds=_clamp(QBOX_WIFI_AGENT_WIFI_CONNECT_TIMEOUT_SECONDS, 8),

        # -------- RETRY --------
        retry_max_attempts=_clamp(QBOX_WIFI_AGENT_RETRY_MAX_ATTEMPTS, 3),
        retry_base_delay_seconds=max(QBOX_WIFI_AGENT_RETRY_BASE_DELAY_SECONDS, 1.0),
        max_retry_delay_seconds=_clamp(QBOX_WIFI_AGENT_MAX_RETRY_DELAY_SECONDS, 30),
        max_batch_size=_clamp(QBOX_WIFI_AGENT_MAX_BATCH_SIZE, 1),

        # -------- STORAGE --------
        state_file=Path(QBOX_WIFI_AGENT_STATE_FILE),
        queue_file=Path(QBOX_WIFI_AGENT_QUEUE_FILE),
    )

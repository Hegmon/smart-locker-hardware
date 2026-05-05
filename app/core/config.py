from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass

from app.deployment.device_identity import ensure_device_id
from app.deployment.runtime_config import (
    get_bool_setting,
    get_float_setting,
    get_int_setting,
    get_path_setting,
    get_str_setting,
)
from app.services.backend_state import load_backend_state
# ---------------- BASIC SYSTEM CONFIG ----------------
APP_HOST = get_str_setting("APP_HOST", "0.0.0.0")
APP_PORT = get_int_setting("APP_PORT", 8000)
APP_VERSION = get_str_setting("APP_VERSION", "v1.0.0")
WIFI_INTERFACE = get_str_setting("WIFI_INTERFACE", "wlan0")
HOTSPOT_CONNECTION = get_str_setting("HOTSPOT_CONNECTION", "SmartLockerHotspot")
HOTSPOT_SSID = get_str_setting("HOTSPOT_SSID", "SmartLocker-Setup")
HOTSPOT_PASSWORD = get_str_setting("HOTSPOT_PASSWORD", "SmartLocker123")
QBOX_DEVICE_NAME = get_str_setting(
    "QBOX_DEVICE_NAME",
    get_str_setting("HOSTNAME", ensure_device_id()),
)[:25]
MQTT_HOST = get_str_setting("MQTT_HOST", "69.62.125.223")
MQTT_PORT = get_int_setting("MQTT_PORT", 1883)
MQTT_KEEPALIVE = get_int_setting("MQTT_KEEPALIVE", 60)
MQTT_USERNAME = get_str_setting("MQTT_USERNAME", "qbox")
MQTT_PASSWORD = get_str_setting("MQTT_PASSWORD", "strongpassword123")

QBOX_DEVICE_REGISTRATION_URL = get_str_setting(
    "QBOX_DEVICE_REGISTRATION_URL",
    "https://backend.qbox.sa/devices/",
)
QBOX_TELEMETRY_URL = get_str_setting(
    "QBOX_TELEMETRY_URL",
    "https://backend.qbox.sa/devices-telemetry/",
)
QBOX_BACKEND_TIMEOUT_SECONDS = get_float_setting("QBOX_BACKEND_TIMEOUT_SECONDS", 10.0)
QBOX_AUTO_REGISTER = get_bool_setting("QBOX_AUTO_REGISTER", True)
LOCKER_DEFAULT_STATUS = get_str_setting("LOCKER_DEFAULT_STATUS", "LOCKED")

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"

QBOX_WIFI_AGENT_BASE_URL = get_str_setting(
    "QBOX_WIFI_AGENT_BASE_URL",
    "https://backend.qbox.sa",
).rstrip("/")
QBOX_WIFI_AGENT_DEVICE_ID = get_str_setting(
    "QBOX_WIFI_AGENT_DEVICE_ID",
    ensure_device_id(),
).strip()
QBOX_WIFI_AGENT_SCAN_INTERVAL_SECONDS = get_int_setting(
    "QBOX_WIFI_AGENT_SCAN_INTERVAL_SECONDS",
    60,
)
QBOX_WIFI_AGENT_HEARTBEAT_SECONDS = get_int_setting(
    "QBOX_WIFI_AGENT_HEARTBEAT_SECONDS",
    300,
)
QBOX_WIFI_AGENT_REQUEST_TIMEOUT_SECONDS = get_float_setting(
    "QBOX_WIFI_AGENT_REQUEST_TIMEOUT_SECONDS",
    15.0,
)
QBOX_WIFI_AGENT_RETRY_MAX_ATTEMPTS = get_int_setting(
    "QBOX_WIFI_AGENT_RETRY_MAX_ATTEMPTS",
    3,
)
QBOX_WIFI_AGENT_RETRY_BASE_DELAY_SECONDS = get_float_setting(
    "QBOX_WIFI_AGENT_RETRY_BASE_DELAY_SECONDS",
    2.0,
)
QBOX_WIFI_AGENT_MAX_BATCH_SIZE = get_int_setting(
    "QBOX_WIFI_AGENT_MAX_BATCH_SIZE",
    20,
)
QBOX_WIFI_AGENT_MAX_RETRY_DELAY_SECONDS = get_int_setting(
    "QBOX_WIFI_AGENT_MAX_RETRY_DELAY_SECONDS",
    300,
)
QBOX_WIFI_AGENT_SIGNAL_CHANGE_THRESHOLD = get_int_setting(
    "QBOX_WIFI_AGENT_SIGNAL_CHANGE_THRESHOLD",
    8,
)
QBOX_WIFI_AGENT_COMMAND_POLL_INTERVAL_SECONDS = get_int_setting(
    "QBOX_WIFI_AGENT_COMMAND_POLL_INTERVAL_SECONDS",
    15,
)
QBOX_WIFI_AGENT_STATE_HEARTBEAT_SECONDS = get_int_setting(
    "QBOX_WIFI_AGENT_STATE_HEARTBEAT_SECONDS",
    300,
)
QBOX_WIFI_AGENT_SCAN_ENDPOINT = get_str_setting(
    "QBOX_WIFI_AGENT_SCAN_ENDPOINT",
    "",
).strip()
QBOX_WIFI_AGENT_STATE_ENDPOINT = get_str_setting(
    "QBOX_WIFI_AGENT_STATE_ENDPOINT",
    "",
).strip()
QBOX_WIFI_AGENT_COMMAND_ENDPOINT = get_str_setting(
    "QBOX_WIFI_AGENT_COMMAND_ENDPOINT",
    "",
).strip()
QBOX_WIFI_AGENT_COMMAND_RESULT_ENDPOINT_TEMPLATE = get_str_setting(
    "QBOX_WIFI_AGENT_COMMAND_RESULT_ENDPOINT_TEMPLATE",
    "",
).strip()
QBOX_WIFI_AGENT_STATE_FILE = str(get_path_setting(
    "QBOX_WIFI_AGENT_STATE_FILE",
    str(CONFIG_DIR / "wifi_agent_state.json"),
))
QBOX_WIFI_AGENT_QUEUE_FILE = str(get_path_setting(
    "QBOX_WIFI_AGENT_QUEUE_FILE",
    str(CONFIG_DIR / "wifi_agent_queue.json"),
))

# ---------------- AGENT CONFIG ----------------
@dataclass(frozen=True)
class AgentConfig:
    # device identity
    base_url: str
    device_uuid: str
    device_id: str
    interface: str

    # mqtt
    mqtt_host: str
    mqtt_port: int
    mqtt_keepalive: int

    # topics
    mqtt_command_topic: str
    mqtt_command_result_topic: str
    mqtt_scan_topic: str
    mqtt_state_topic: str


# ---------------- LOAD CONFIG ----------------
def load_agent_config() -> AgentConfig:
    backend_state = load_backend_state()

    resolved_device_uuid = (
        str(backend_state.get("device_uuid") or "").strip()
        or QBOX_WIFI_AGENT_DEVICE_ID
        or str(backend_state.get("device_id") or "").strip()
    )

    resolved_device_id = (
        str(backend_state.get("device_id") or "").strip()
        or resolved_device_uuid
    )

    if not resolved_device_uuid:
        raise RuntimeError(
            "Device UUID missing. Register device or set QBOX_WIFI_AGENT_DEVICE_ID."
        )

    base_url = QBOX_WIFI_AGENT_BASE_URL

    print(f"[DEBUG] MQTT_HOST: {MQTT_HOST}, MQTT_USERNAME: {MQTT_USERNAME}, device_id: {resolved_device_id}")
    return AgentConfig(
        base_url=base_url,
        device_uuid=resolved_device_uuid,
        device_id=resolved_device_id,
        interface=WIFI_INTERFACE,

        mqtt_host=MQTT_HOST,
        mqtt_port=MQTT_PORT,
        mqtt_keepalive=MQTT_KEEPALIVE,

        mqtt_command_topic=f"devices/{resolved_device_id}/command",
        mqtt_command_result_topic=f"devices/{resolved_device_id}/command/result",
        mqtt_scan_topic=f"devices/{resolved_device_id}/events/scan",
        mqtt_state_topic=f"devices/{resolved_device_id}/events/state",
    )

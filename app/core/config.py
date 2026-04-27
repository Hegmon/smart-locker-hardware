import os
from pathlib import Path


APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
APP_VERSION = os.getenv("APP_VERSION", "v1.0.0")
WIFI_INTERFACE = os.getenv("WIFI_INTERFACE", "wlan0")
HOTSPOT_CONNECTION = os.getenv("HOTSPOT_CONNECTION", "SmartLockerHotspot")
HOTSPOT_SSID = os.getenv("HOTSPOT_SSID", "SmartLocker-Setup")
HOTSPOT_PASSWORD = os.getenv("HOTSPOT_PASSWORD", "SmartLocker123")
QBOX_DEVICE_NAME = os.getenv("QBOX_DEVICE_NAME", os.getenv("HOSTNAME", "QboxPi4"))[:25]
QBOX_DEVICE_REGISTRATION_URL = os.getenv(
    "QBOX_DEVICE_REGISTRATION_URL",
    "https://backend.qbox.sa/devices/",
)
QBOX_TELEMETRY_URL = os.getenv(
    "QBOX_TELEMETRY_URL",
    "https://backend.qbox.sa/devices-telemetry/",
)
QBOX_BACKEND_TIMEOUT_SECONDS = float(os.getenv("QBOX_BACKEND_TIMEOUT_SECONDS", "10"))
QBOX_AUTO_REGISTER = os.getenv("QBOX_AUTO_REGISTER", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
LOCKER_DEFAULT_STATUS = os.getenv("LOCKER_DEFAULT_STATUS", "LOCKED")

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"

QBOX_WIFI_AGENT_BASE_URL = os.getenv(
    "QBOX_WIFI_AGENT_BASE_URL",
    "https://backend.qbox.sa",
).rstrip("/")
QBOX_WIFI_AGENT_DEVICE_ID = os.getenv("QBOX_WIFI_AGENT_DEVICE_ID", "").strip()
QBOX_WIFI_AGENT_SCAN_INTERVAL_SECONDS = int(
    os.getenv("QBOX_WIFI_AGENT_SCAN_INTERVAL_SECONDS", "60")
)
QBOX_WIFI_AGENT_HEARTBEAT_SECONDS = int(
    os.getenv("QBOX_WIFI_AGENT_HEARTBEAT_SECONDS", "300")
)
QBOX_WIFI_AGENT_REQUEST_TIMEOUT_SECONDS = float(
    os.getenv("QBOX_WIFI_AGENT_REQUEST_TIMEOUT_SECONDS", "15")
)
QBOX_WIFI_AGENT_RETRY_MAX_ATTEMPTS = int(
    os.getenv("QBOX_WIFI_AGENT_RETRY_MAX_ATTEMPTS", "3")
)
QBOX_WIFI_AGENT_RETRY_BASE_DELAY_SECONDS = float(
    os.getenv("QBOX_WIFI_AGENT_RETRY_BASE_DELAY_SECONDS", "2")
)
QBOX_WIFI_AGENT_MAX_BATCH_SIZE = int(
    os.getenv("QBOX_WIFI_AGENT_MAX_BATCH_SIZE", "20")
)
QBOX_WIFI_AGENT_MAX_RETRY_DELAY_SECONDS = int(
    os.getenv("QBOX_WIFI_AGENT_MAX_RETRY_DELAY_SECONDS", "300")
)
QBOX_WIFI_AGENT_STATE_FILE = os.getenv(
    "QBOX_WIFI_AGENT_STATE_FILE",
    str(CONFIG_DIR / "wifi_agent_state.json"),
)
QBOX_WIFI_AGENT_QUEUE_FILE = os.getenv(
    "QBOX_WIFI_AGENT_QUEUE_FILE",
    str(CONFIG_DIR / "wifi_agent_queue.json"),
)

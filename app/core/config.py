import os


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
    "https://backend.qbox.sa/hardware-devices/",
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

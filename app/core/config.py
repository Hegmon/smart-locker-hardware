import os


APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
WIFI_INTERFACE = os.getenv("WIFI_INTERFACE", "wlan0")
HOTSPOT_CONNECTION = os.getenv("HOTSPOT_CONNECTION", "SmartLockerHotspot")
HOTSPOT_SSID = os.getenv("HOTSPOT_SSID", "SmartLocker-Setup")
HOTSPOT_PASSWORD = os.getenv("HOTSPOT_PASSWORD", "SmartLocker123")

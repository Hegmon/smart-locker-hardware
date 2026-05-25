from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

import psutil

from app.utils.logger import get_logger


logger = get_logger(__name__)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def cpu_temperature() -> float | None:
    thermal_path = "/sys/class/thermal/thermal_zone0/temp"
    try:
        if os.path.exists(thermal_path):
            with open(thermal_path, "r", encoding="utf-8") as temp_file:
                return round(int(temp_file.read().strip()) / 1000.0, 1)
    except Exception:
        logger.debug("CPU temperature file read failed", exc_info=True)

    try:
        temperatures = psutil.sensors_temperatures()
    except Exception:
        return None
    for entries in temperatures.values():
        for entry in entries:
            if entry.current is not None:
                return round(float(entry.current), 1)
    return None


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1.0)
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return ""


def network_connected(timeout_seconds: float = 1.5) -> bool:
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def wifi_ssid(interface: str = "wlan0") -> str:
    commands = (
        ["iwgetid", interface, "-r"],
        ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
    )
    for command in commands:
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        output = result.stdout.strip()
        if not output:
            continue
        if command[0] == "nmcli":
            for line in output.splitlines():
                active, _, ssid = line.partition(":")
                if active == "yes" and ssid:
                    return ssid
            continue
        return output
    return ""


def telemetry_snapshot(device_id: str, *, wifi_interface: str = "wlan0") -> dict[str, Any]:
    disk = shutil.disk_usage("/")
    return {
        "device_id": device_id,
        "cpu_usage": round(psutil.cpu_percent(interval=None), 1),
        "ram_usage": round(psutil.virtual_memory().percent, 1),
        "disk_usage": round((disk.used / disk.total) * 100, 1) if disk.total else 0.0,
        "cpu_temperature": cpu_temperature(),
        "uptime_seconds": int(time.time() - psutil.boot_time()),
        "network_connected": network_connected(),
        "local_ip": local_ip(),
        "wifi_ssid": wifi_ssid(wifi_interface),
        "timestamp": utc_timestamp(),
    }

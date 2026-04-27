import os
import shutil
import subprocess
import time
from glob import glob
from typing import Any


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None


def _read_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_camera_devices() -> list[str]:
    return sorted(glob("/dev/video*"))


def get_camera_inventory() -> dict[str, Any]:
    internal_device = os.getenv("INTERNAL_CAMERA_DEVICE", "/dev/video0")
    external_device = os.getenv("EXTERNAL_CAMERA_DEVICE", "/dev/video2")

    devices = get_camera_devices()
    v4l2_available = shutil.which("v4l2-ctl") is not None

    return {
        "detected_devices": devices,
        "internal_camera": {
            "device": internal_device,
            "connected": os.path.exists(internal_device),
        },
        "external_camera": {
            "device": external_device,
            "connected": os.path.exists(external_device),
        },
        "v4l2_tools_available": v4l2_available,
    }


def get_light_status() -> dict[str, Any]:
    gpiochip_available = os.path.exists("/dev/gpiochip0")
    configured_pin = os.getenv("LIGHT_GPIO_PIN", "")
    default_state = os.getenv("LIGHT_DEFAULT_STATE", "unknown")

    return {
        "gpio_available": gpiochip_available,
        "configured_pin": configured_pin,
        "state": default_state,
        "note": "State is reported from configuration until light control logic is added.",
    }


def get_system_hardware_status() -> dict[str, Any]:
    libcamera_result = _run_command(["libcamera-hello", "--list-cameras"])
    libcamera_available = libcamera_result is not None
    libcamera_detected = (
        bool(libcamera_result and libcamera_result.returncode == 0 and libcamera_result.stdout.strip())
    )

    return {
        "lights": get_light_status(),
        "cameras": get_camera_inventory(),
        "system_metrics": get_system_metrics(),
        "platform": {
            "gpiochip0_present": os.path.exists("/dev/gpiochip0"),
            "libcamera_available": libcamera_available,
            "libcamera_detected_cameras": libcamera_detected,
        },
    }


def _read_cpu_times() -> tuple[int, int]:
    with open("/proc/stat", "r", encoding="utf-8") as stat_file:
        first_line = stat_file.readline().strip()

    parts = first_line.split()
    values = [int(value) for value in parts[1:]]
    idle = values[3] + values[4]
    total = sum(values)
    return idle, total


def get_cpu_usage_percent(sample_seconds: float = 0.2) -> float:
    idle_before, total_before = _read_cpu_times()
    time.sleep(sample_seconds)
    idle_after, total_after = _read_cpu_times()

    total_delta = total_after - total_before
    idle_delta = idle_after - idle_before
    if total_delta <= 0:
        return 0.0
    return round((1 - (idle_delta / total_delta)) * 100, 2)


def get_ram_usage_percent() -> float:
    memory_values: dict[str, int] = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as meminfo_file:
        for line in meminfo_file:
            key, value = line.split(":", maxsplit=1)
            memory_values[key] = int(value.strip().split()[0])

    total = memory_values.get("MemTotal", 0)
    available = memory_values.get("MemAvailable", 0)
    if total <= 0:
        return 0.0
    used = total - available
    return round((used / total) * 100, 2)


def get_system_metrics() -> dict[str, float]:
    return {
        "cpu_usage": get_cpu_usage_percent(),
        "ram_usage": get_ram_usage_percent(),
    }

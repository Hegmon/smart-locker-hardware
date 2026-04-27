import os
import shutil
import subprocess
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
        "platform": {
            "gpiochip0_present": os.path.exists("/dev/gpiochip0"),
            "libcamera_available": libcamera_available,
            "libcamera_detected_cameras": libcamera_detected,
        },
    }

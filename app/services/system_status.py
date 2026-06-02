from __future__ import annotations

import json
import subprocess
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from app.core.mqtt_manager import get_shared_mqtt_manager
from app.deployment.device_identity import ensure_device_id
from app.deployment.runtime_config import get_int_setting, get_str_setting
from app.inspection_agent.hardware.camera_controller import CameraController
from app.services.qbox_runtime import get_qbox_runtime_state
from app.utils.logger import get_logger
from app.utils.system_info import utc_timestamp


logger = get_logger(__name__)


def _health_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/health"


def _service_ok(port: int) -> bool:
    try:
        with urlopen(_health_url(port), timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, URLError):
        return False
    return str(payload.get("status", "")).lower() == "ok"


def _service_running(service_name: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", service_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=3.0,
        )
        return result.returncode == 0
    except Exception:
        logger.debug("systemctl status check failed for %s", service_name, exc_info=True)
        return False


def _camera_healthy(role: str, controller: CameraController) -> bool:
    try:
        result = controller.capture_frame(role)
        return bool(result.captured)
    except Exception:
        logger.exception("Camera health check failed for role=%s", role)
        return False


def _camera_status(is_healthy: bool) -> str:
    return "working" if is_healthy else "not working"


def build_system_status() -> dict[str, Any]:
    hardware_port = get_int_setting("HARDWARE_AGENT_HEALTH_PORT", 8091)
    streaming_port = get_int_setting("STREAMING_AGENT_HEALTH_PORT", 8092)
    service_name = get_str_setting("SMARTLOCKER_SERVICE_NAME", "qbox-device.service")
    mqtt_manager = get_shared_mqtt_manager()
    runtime_state = get_qbox_runtime_state().snapshot()
    camera_controller = CameraController()
    internal_camera_healthy = _camera_healthy("internal", camera_controller)
    external_camera_healthy = _camera_healthy("external", camera_controller)
    service_status = "running" if _service_running(service_name) else "unhealthy"
    mqtt_connected = mqtt_manager.is_connected()
    mqtt_status = mqtt_manager.mqtt_status()
    qbox_status = "Offline"
    if service_status == "running" and mqtt_connected and internal_camera_healthy and external_camera_healthy:
        qbox_status = "Online"
    logger.info(
        "Generated qbox system status service_status=%s mqtt_status=%s mqtt_connected=%s internal_camera=%s external_camera=%s qbox_status=%s",
        service_status,
        mqtt_status,
        mqtt_connected,
        internal_camera_healthy,
        external_camera_healthy,
        qbox_status,
    )
    return {
        "device_id": ensure_device_id(),
        "hardware": "ok" if _service_ok(hardware_port) else "error",
        "streaming": "ok" if _service_ok(streaming_port) else "error",
        "registry": "ok",
        "mqtt_status": mqtt_status,
        "mqtt_connected": mqtt_connected,
        "internal_camera_status": _camera_status(internal_camera_healthy),
        "external_camera_status": _camera_status(external_camera_healthy),
        "qbox_status": qbox_status,
        "alarm_active": bool(runtime_state.get("alarm_active")),
        "last_mqtt_reconnect": runtime_state.get("last_mqtt_reconnect") or "",
        "service_status": service_status,
        "timestamp": utc_timestamp(),
    }

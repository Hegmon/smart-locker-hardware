from __future__ import annotations

import json
import subprocess
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from app.core.mqtt_manager import get_shared_mqtt_manager
from app.deployment.device_identity import ensure_device_id
from app.deployment.runtime_config import get_int_setting, get_str_setting
from app.services.qbox_runtime import get_qbox_runtime_state
from app.services.hardware_manager import get_camera_inventory
from app.services.runtime_registry import get_streaming_agent
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


def _camera_status(is_healthy: bool) -> str:
    return "working" if is_healthy else "not working"


def _streaming_camera_health() -> tuple[bool | None, bool | None]:
    streaming_agent = get_streaming_agent()
    if streaming_agent is None:
        return None, None

    runtime_agent = getattr(streaming_agent, "_agent", streaming_agent)
    stream_manager = getattr(runtime_agent, "stream_manager", None)
    if stream_manager is None:
        return None, None

    try:
        stream_status = stream_manager.get_stream_status() or {}
    except Exception:
        logger.debug("Streaming status lookup failed", exc_info=True)
        return None, None

    internal_running = stream_status.get("internal", {}).get("running")
    external_running = stream_status.get("external", {}).get("running")
    if internal_running is None and external_running is None:
        return None, None
    return bool(internal_running), bool(external_running)


def build_system_status() -> dict[str, Any]:
    hardware_port = get_int_setting("HARDWARE_AGENT_HEALTH_PORT", 8091)
    streaming_port = get_int_setting("STREAMING_AGENT_HEALTH_PORT", 8092)
    service_name = get_str_setting("SMARTLOCKER_SERVICE_NAME", "qbox-device.service")
    mqtt_manager = get_shared_mqtt_manager()
    runtime_state = get_qbox_runtime_state().snapshot()
    internal_camera_healthy, external_camera_healthy = _streaming_camera_health()
    if internal_camera_healthy is None or external_camera_healthy is None:
        camera_inventory = get_camera_inventory()
        internal_camera_healthy = bool(camera_inventory.get("internal_camera", {}).get("connected"))
        external_camera_healthy = bool(camera_inventory.get("external_camera", {}).get("connected"))
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

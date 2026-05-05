from __future__ import annotations

import json
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from app.deployment.device_identity import ensure_device_id
from app.deployment.runtime_config import get_int_setting


def _health_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/health"


def _service_ok(port: int) -> bool:
    try:
        with urlopen(_health_url(port), timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, URLError):
        return False
    return str(payload.get("status", "")).lower() == "ok"


def build_system_status() -> dict[str, Any]:
    hardware_port = get_int_setting("HARDWARE_AGENT_HEALTH_PORT", 8091)
    streaming_port = get_int_setting("STREAMING_AGENT_HEALTH_PORT", 8092)
    return {
        "device_id": ensure_device_id(),
        "hardware": "ok" if _service_ok(hardware_port) else "error",
        "streaming": "ok" if _service_ok(streaming_port) else "error",
        "registry": "ok",
    }

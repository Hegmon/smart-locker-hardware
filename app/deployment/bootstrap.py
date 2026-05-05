from __future__ import annotations

from pathlib import Path

from app.deployment.device_identity import ensure_device_id
from app.deployment.runtime_config import get_bool_setting, get_str_setting
from app.services.wifi_manager import WifiCommandError, connect_wifi, get_connected_wifi_details
from app.utils.logger import get_logger


logger = get_logger(__name__)


RUNTIME_DIRECTORIES = (
    Path("/etc/smartlocker"),
    Path("/var/lib/smartlocker"),
    Path("/var/log/smartlocker"),
)
RUNTIME_FILES = (
    Path("/var/log/smartlocker/bootstrap.log"),
    Path("/var/log/smartlocker/device-registry.log"),
    Path("/var/log/smartlocker/hardware-agent.log"),
    Path("/var/log/smartlocker/streaming-agent.log"),
)


def ensure_runtime_directories() -> None:
    for path in RUNTIME_DIRECTORIES:
        path.mkdir(parents=True, exist_ok=True)
    for path in RUNTIME_FILES:
        path.touch(exist_ok=True)
        path.chmod(0o644)


def maybe_configure_wifi() -> dict[str, str] | None:
    if not get_bool_setting("SMARTLOCKER_BOOTSTRAP_WIFI", True):
        return None

    current = get_connected_wifi_details()
    if current.get("connected_ssid"):
        return {"status": "already_connected", "ssid": str(current.get("connected_ssid"))}

    ssid = get_str_setting("WIFI_SSID", aliases=("SMARTLOCKER_WIFI_SSID",))
    password = get_str_setting("WIFI_PASSWORD", aliases=("SMARTLOCKER_WIFI_PASSWORD",))
    if not ssid:
        return {"status": "skipped", "reason": "wifi credentials not provided"}

    try:
        return connect_wifi(ssid, password)
    except WifiCommandError as exc:
        logger.warning("Boot WiFi configuration failed: %s", exc)
        return {"status": "failed", "reason": str(exc), "ssid": ssid}


def bootstrap_device() -> dict[str, object]:
    ensure_runtime_directories()
    device_id = ensure_device_id()
    wifi_result = maybe_configure_wifi()
    return {
        "device_id": device_id,
        "wifi": wifi_result,
    }


def main() -> int:
    result = bootstrap_device()
    logger.info("Bootstrap completed: %s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

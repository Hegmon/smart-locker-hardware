from dataclasses import dataclass
from typing import Optional, Dict, Any


@dataclass
class BLERequest:
    action: str
    ssid: Optional[str] = None
    password: Optional[str] = None
    response: str = "full"
    include_scan_wifi: bool = True
    include_networks: bool = True
    max_networks: Optional[int] = None


class BLEProtocolError(Exception):
    pass


def parse_ble_request(payload: Dict[str, Any]) -> BLERequest:
    action = (
        payload.get("action")
        or payload.get("command")
        or payload.get("type")
    )
    if not action:
        raise BLEProtocolError("missing action field")

    action = str(action).strip().lower()

    if action in {"connect_wifi", "wifi_connect", "connect"}:
        return BLERequest(
            action="connect_wifi",
            ssid=payload.get("ssid"),
            password=payload.get("password", ""),
            response=str(payload.get("response") or "full").strip().lower(),
            include_scan_wifi=bool(payload.get("include_scan_wifi", True)),
            include_networks=bool(payload.get("include_networks", True)),
            max_networks=_optional_int(payload.get("max_networks")),
        )
    if action in {"scan_wifi", "wifi_scan", "scan"}:
        return BLERequest(
            action="scan_wifi",
            max_networks=_optional_int(payload.get("max_networks")),
        )
    raise BLEProtocolError(f"unknown action: {action}")


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

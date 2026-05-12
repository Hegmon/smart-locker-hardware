from dataclasses import dataclass
from typing import Optional, Dict, Any


@dataclass
class BLERequest:
    action: str
    ssid: Optional[str] = None
    password: Optional[str] = None


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
        )
    if action in {"scan_wifi", "wifi_scan", "scan"}:
        return BLERequest(action="scan_wifi")
    raise BLEProtocolError(f"unknown action: {action}")

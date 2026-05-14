import time
from typing import Any, Callable, Dict

from app.services.wifi_manager import connect_wifi, get_connected_wifi_details
from app.hardware_agent.scanner import WifiScanner
from app.hardware_agent.provisioning.ble.protocol import parse_ble_request

BLE_CONNECT_RESPONSE_GRACE_SECONDS = 2.0


class BLEHandler:
    def __init__(
        self,
        interface: str,
        on_wifi_connected: Callable[[str], bool | None] | None = None,
    ):
        self.scanner = WifiScanner(interface)
        self._on_wifi_connected = on_wifi_connected
        self._pending_connected_ssid: str | None = None

    # ===================== ENTRY =====================
    def handle(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            request = parse_ble_request(payload)

            if request.action == "scan_wifi":
                return self._scan_wifi()

            if request.action == "connect_wifi":
                return self._connect_wifi(request.ssid, request.password)

            return {"error": "unsupported_action"}

        except Exception as e:
            return {"status": False, "error": f"internal_error: {str(e)}"}

    def after_response_sent(self) -> None:
        if not self._pending_connected_ssid or not self._on_wifi_connected:
            return

        ssid = self._pending_connected_ssid
        self._pending_connected_ssid = None
        time.sleep(BLE_CONNECT_RESPONSE_GRACE_SECONDS)
        self._on_wifi_connected(ssid)

    # ===================== SCAN =====================
    def _scan_wifi(self) -> Dict[str, Any]:
        networks = self.scanner.scan()

        return {
            "status": "success",
            "networks": [
                {
                    "ssid": n.ssid,
                    "rssi": n.rssi,
                    "security": n.security,
                    "secured": n.is_secured,
                }
                for n in networks
            ]
        }

    # ===================== CONNECT =====================
    def _connect_wifi(self, ssid: str, password: str) -> Dict[str, Any]:
        if not ssid:
            return {"action": "connect_wifi", "status": False, "error": "ssid_required"}

        try:
            result = connect_wifi(ssid, password)
            connection = result.get("connection") if isinstance(result, dict) else {}
            connection = connection or get_connected_wifi_details()
            if not connection.get("connected_ssid"):
                return {
                    "action": "connect_wifi",
                    "status": False,
                    "ssid": ssid,
                    "error": "connection_failed",
                    "connection": connection,
                }

            scan_response = self._scan_wifi()
            self._pending_connected_ssid = ssid
            return {
                "action": "connect_wifi",
                "status": True,
                "ssid": ssid,
                "message": "connected",
                "connection": result,
                "scan_wifi": scan_response,
                "networks": scan_response.get("networks", []),
            }

        except Exception as e:
            return {
                "action": "connect_wifi",
                "status": False,
                "ssid": ssid,
                "error": str(e),
            }

    # ===================== STATUS =====================
    def status(self) -> Dict[str, Any]:
        return get_connected_wifi_details()

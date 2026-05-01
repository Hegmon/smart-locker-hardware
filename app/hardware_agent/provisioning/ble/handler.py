from typing import Any, Callable, Dict

from app.services.wifi_manager import connect_wifi, get_connected_wifi_details
from app.hardware_agent.scanner import WifiScanner
from app.hardware_agent.provisioning.ble.protocol import parse_ble_request


class BLEHandler:
    def __init__(
        self,
        interface: str,
        on_wifi_connected: Callable[[str], None] | None = None,
    ):
        self.scanner = WifiScanner(interface)
        self._on_wifi_connected = on_wifi_connected

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
            return {"error": f"internal_error: {str(e)}"}

    # ===================== SCAN =====================
    def _scan_wifi(self) -> Dict[str, Any]:
        networks = self.scanner.scan()

        return {
            "networks": [
                {
                    "ssid": n.ssid,
                    "rssi": n.rssi,
                    "secured": n.is_secured,
                }
                for n in networks
            ]
        }

    # ===================== CONNECT =====================
    def _connect_wifi(self, ssid: str, password: str) -> Dict[str, Any]:
        if not ssid:
            return {"error": "ssid_required"}

        try:
            result = connect_wifi(ssid, password)
            if self._on_wifi_connected:
                try:
                    self._on_wifi_connected(ssid)
                except Exception:
                    pass

            return {
                "status": "success",
                "ssid": ssid,
                "connection": result,
            }

        except Exception as e:
            return {
                "status": "failed",
                "error": str(e),
            }

    # ===================== STATUS =====================
    def status(self) -> Dict[str, Any]:
        return get_connected_wifi_details()

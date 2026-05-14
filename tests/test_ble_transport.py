from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import Mock, patch

try:
    import dbus

    from app.hardware_agent.provisioning.ble.advertisement import Advertisement
    from app.hardware_agent.provisioning.ble.characteristic import (
        BLE_RESPONSE_CHUNK_SIZE_BYTES,
        GATT_CHARACTERISTIC_IFACE,
        ResponseCharacteristic,
    )
    from app.hardware_agent.provisioning.ble.service import SERVICE_UUID
    from app.hardware_agent.provisioning.ble.server import BLEServer
except ImportError as exc:
    dbus = None
    Advertisement = None
    BLE_RESPONSE_CHUNK_SIZE_BYTES = 0
    GATT_CHARACTERISTIC_IFACE = ""
    ResponseCharacteristic = None
    SERVICE_UUID = ""
    BLEServer = None
    DBUS_IMPORT_ERROR = exc
else:
    DBUS_IMPORT_ERROR = None


class BLETransportTests(unittest.TestCase):
    def test_advertisement_exposes_local_name_and_service_uuid(self) -> None:
        if Advertisement is None or dbus is None:
            self.skipTest(f"BLE dependencies unavailable: {DBUS_IMPORT_ERROR}")

        advertisement = Advertisement.__new__(Advertisement)
        advertisement.path = "/org/bluez/smartlocker/advertisement0"
        advertisement.service_uuids = [SERVICE_UUID]
        advertisement.local_name = "SmartLocker-ABC123"
        advertisement.includes = ["tx-power"]
        advertisement.appearance = 0

        props = advertisement.get_properties()["org.bluez.LEAdvertisement1"]

        self.assertEqual(str(props["Type"]), "peripheral")
        self.assertEqual([str(uuid) for uuid in props["ServiceUUIDs"]], [SERVICE_UUID])
        self.assertEqual(str(props["LocalName"]), "SmartLocker-ABC123")
        self.assertNotIn("Discoverable", props)

    def test_advertisement_ready_keeps_classic_discovery_disabled(self) -> None:
        if BLEServer is None or dbus is None:
            self.skipTest(f"BLE dependencies unavailable: {DBUS_IMPORT_ERROR}")

        server = BLEServer.__new__(BLEServer)
        server._lock = threading.RLock()
        server._stop_requested = False
        server._advertising_active = False
        adapter = Mock()

        with patch.object(server, "_get_adapter", return_value=adapter):
            server._on_advertisement_ready()

        set_calls = [
            (call.args[1], bool(call.args[2]))
            for call in adapter.Set.call_args_list
            if call.args[0] == "org.bluez.Adapter1"
        ]
        self.assertIn(("Pairable", False), set_calls)
        self.assertIn(("Discoverable", False), set_calls)
        self.assertTrue(server._advertising_active)

    def test_response_notifications_are_chunked_compact_json(self) -> None:
        if ResponseCharacteristic is None or dbus is None:
            self.skipTest(f"BLE dependencies unavailable: {DBUS_IMPORT_ERROR}")

        response_char = ResponseCharacteristic.__new__(ResponseCharacteristic)
        response_char.notifying = True
        response_char.value = b""
        response_char.PropertiesChanged = Mock()
        payload = {
            "status": "success",
            "networks": [
                {
                    "ssid": f"network-{index}",
                    "rssi": -40 - index,
                    "security": "WPA2",
                    "secured": True,
                }
                for index in range(20)
            ],
        }

        response_char.notify(payload)

        expected = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        notified_chunks = [
            bytes(int(byte) for byte in call.args[1]["Value"])
            for call in response_char.PropertiesChanged.call_args_list
            if call.args[0] == GATT_CHARACTERISTIC_IFACE and "Value" in call.args[1]
        ]

        self.assertGreater(len(notified_chunks), 1)
        self.assertEqual(b"".join(notified_chunks), expected)
        self.assertEqual(response_char.value, expected)
        self.assertTrue(all(len(chunk) <= BLE_RESPONSE_CHUNK_SIZE_BYTES for chunk in notified_chunks))
        self.assertEqual(json.loads(expected.decode("utf-8")), payload)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

try:
    from app.hardware_agent.provisioning.ble.characteristic import _redact_payload
except ImportError as exc:
    _redact_payload = None
    DBUS_IMPORT_ERROR = exc
else:
    DBUS_IMPORT_ERROR = None


class BLERedactionTests(unittest.TestCase):
    def test_password_is_redacted_in_nested_payload(self) -> None:
        if _redact_payload is None:
            self.skipTest(f"BLE dependencies unavailable: {DBUS_IMPORT_ERROR}")
        payload = {
            "action": "connect_wifi",
            "ssid": "office",
            "password": "plain-secret",
            "connection": {"psk": "another-secret"},
        }

        redacted = _redact_payload(payload)

        self.assertEqual(redacted["password"], "********")
        self.assertEqual(redacted["connection"]["psk"], "********")

if __name__ == "__main__":
    unittest.main()

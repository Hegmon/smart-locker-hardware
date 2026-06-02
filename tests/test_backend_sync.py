from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services.backend_sync import get_backend_sync_status, send_telemetry


class BackendSyncTests(unittest.TestCase):
    def test_get_backend_sync_status_includes_live_health(self) -> None:
        live_status = {
            "mqtt_status": "connected",
            "mqtt_connected": True,
            "internal_camera_status": "working",
            "external_camera_status": "working",
            "qbox_status": "Online",
            "alarm_active": False,
        }

        with (
            patch("app.services.backend_sync.load_backend_state", return_value={"device_uuid": "uuid-1", "status": "Online"}),
            patch("app.services.backend_sync.ensure_device_id", return_value="device-1"),
            patch("app.services.backend_sync.build_system_status", return_value=live_status),
        ):
            payload = get_backend_sync_status()

        self.assertEqual(payload["device_uuid"], "uuid-1")
        self.assertEqual(payload["device_id"], "device-1")
        self.assertEqual(payload["status"], "Online")
        self.assertEqual(payload["mqtt_status"], "connected")
        self.assertTrue(payload["mqtt_connected"])
        self.assertTrue(payload["mqtt_online"])
        self.assertEqual(payload["internal_camera_status"], "working")
        self.assertEqual(payload["external_camera_status"], "working")
        self.assertEqual(payload["qbox_status"], "Online")
        self.assertFalse(payload["alarm_active"])

    def test_send_telemetry_persists_live_health_snapshot(self) -> None:
        response = SimpleNamespace(content=b"{}", json=Mock(return_value={}), raise_for_status=Mock(return_value=None))
        live_status = {
            "mqtt_status": "connected",
            "mqtt_connected": True,
            "internal_camera_status": "working",
            "external_camera_status": "working",
            "qbox_status": "Online",
            "alarm_active": True,
        }
        saved_state: dict[str, object] = {}

        with (
            patch("app.services.backend_sync.build_telemetry_payload", return_value={"device": "uuid-1"}),
            patch("app.services.backend_sync.build_system_status", return_value=live_status),
            patch("app.services.backend_sync.load_backend_state", return_value={}),
            patch("app.services.backend_sync.save_backend_state", side_effect=lambda state: saved_state.update(state)),
            patch("app.services.backend_sync.requests.post", return_value=response) as post_mock,
            patch("app.services.backend_sync._utc_now_iso", return_value="2026-06-02T07:42:13Z"),
        ):
            result = send_telemetry()

        post_mock.assert_called_once()
        self.assertTrue(result["sent"])
        self.assertEqual(result["device_uuid"], "uuid-1")
        self.assertEqual(saved_state["last_seen_at"], "2026-06-02T07:42:13Z")
        self.assertEqual(saved_state["last_health_at"], "2026-06-02T07:42:13Z")
        self.assertEqual(saved_state["status"], "Online")
        self.assertEqual(saved_state["mqtt_status"], "connected")
        self.assertTrue(saved_state["mqtt_connected"])
        self.assertTrue(saved_state["mqtt_online"])
        self.assertEqual(saved_state["internal_camera_status"], "working")
        self.assertEqual(saved_state["external_camera_status"], "working")
        self.assertEqual(saved_state["qbox_status"], "Online")
        self.assertTrue(saved_state["alarm_active"])


if __name__ == "__main__":
    unittest.main()

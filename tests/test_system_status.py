from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services.system_status import build_system_status


class SystemStatusTests(unittest.TestCase):
    def test_build_system_status_includes_alarm_mqtt_and_camera_health(self) -> None:
        fake_manager = SimpleNamespace(is_connected=Mock(return_value=True), mqtt_status=Mock(return_value="connected"))
        fake_stream_manager = SimpleNamespace(
            get_stream_status=lambda: {
                "internal": {"running": True},
                "external": {"running": False},
            }
        )
        fake_streaming_agent = SimpleNamespace(_agent=SimpleNamespace(stream_manager=fake_stream_manager))

        fake_runtime = SimpleNamespace(snapshot=lambda: {"alarm_active": True, "last_mqtt_reconnect": "2026-06-02T00:00:00+00:00"})

        with (
            patch("app.services.system_status.get_shared_mqtt_manager", return_value=fake_manager),
            patch("app.services.system_status.get_streaming_agent", return_value=fake_streaming_agent),
            patch("app.services.system_status.get_camera_inventory", return_value={}),
            patch("app.services.system_status._service_ok", return_value=True),
            patch("app.services.system_status._service_running", return_value=True),
            patch("app.services.system_status.get_qbox_runtime_state", return_value=fake_runtime),
            patch("app.services.system_status.ensure_device_id", return_value="pi-uuid"),
        ):
            payload = build_system_status()

        self.assertEqual(payload["device_id"], "pi-uuid")
        self.assertEqual(payload["mqtt_status"], "connected")
        self.assertTrue(payload["mqtt_connected"])
        self.assertTrue(payload["alarm_active"])
        self.assertEqual(payload["last_mqtt_reconnect"], "2026-06-02T00:00:00+00:00")
        self.assertEqual(payload["internal_camera_status"], "working")
        self.assertEqual(payload["external_camera_status"], "not working")
        self.assertEqual(payload["service_status"], "running")
        self.assertEqual(payload["qbox_status"], "Offline")

    def test_build_system_status_reports_offline_when_service_unhealthy(self) -> None:
        fake_manager = SimpleNamespace(is_connected=Mock(return_value=False), mqtt_status=Mock(return_value="disconnected"))
        fake_stream_manager = SimpleNamespace(
            get_stream_status=lambda: {
                "internal": {"running": True},
                "external": {"running": True},
            }
        )
        fake_streaming_agent = SimpleNamespace(_agent=SimpleNamespace(stream_manager=fake_stream_manager))

        fake_runtime = SimpleNamespace(snapshot=lambda: {"alarm_active": False, "last_mqtt_reconnect": ""})

        with (
            patch("app.services.system_status.get_shared_mqtt_manager", return_value=fake_manager),
            patch("app.services.system_status.get_streaming_agent", return_value=fake_streaming_agent),
            patch("app.services.system_status.get_camera_inventory", return_value={}),
            patch("app.services.system_status._service_ok", return_value=True),
            patch("app.services.system_status._service_running", return_value=False),
            patch("app.services.system_status.get_qbox_runtime_state", return_value=fake_runtime),
            patch("app.services.system_status.ensure_device_id", return_value="pi-uuid"),
        ):
            payload = build_system_status()

        self.assertEqual(payload["service_status"], "unhealthy")
        self.assertEqual(payload["internal_camera_status"], "working")
        self.assertEqual(payload["external_camera_status"], "working")
        self.assertEqual(payload["qbox_status"], "Offline")


if __name__ == "__main__":
    unittest.main()

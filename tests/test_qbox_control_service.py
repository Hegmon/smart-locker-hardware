from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services.qbox_control import QBoxControlService
from app.services.qbox_runtime import get_qbox_runtime_state


class QBoxControlServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_state = get_qbox_runtime_state()
        self.runtime_state.set_alarm_active(False)
        self.runtime_state.set_last_mqtt_reconnect("")

        self.mqtt = SimpleNamespace(
            device_id="pi-uuid",
            loads=lambda payload: json.loads(payload.decode("utf-8")),
            publish_json=Mock(return_value=True),
            restart_connection=Mock(return_value=True),
            subscribe=Mock(),
        )
        self.relay = SimpleNamespace(
            start=Mock(),
            red_led_on=Mock(),
            red_led_off=Mock(),
            buzzer_on=Mock(),
            buzzer_off=Mock(),
        )
        self.service = QBoxControlService(self.mqtt, relay_controller=self.relay)

    def test_alarm_start_publishes_ack_and_turns_on_hardware(self) -> None:
        self.service._handle_alarm_message(
            "qbox/pi-uuid/alarm/control",
            b'{"action":"start"}',
        )

        self.relay.red_led_on.assert_called_once()
        self.relay.buzzer_on.assert_called_once()
        self.mqtt.publish_json.assert_called_once()
        topic, payload = self.mqtt.publish_json.call_args.args[:2]
        self.assertEqual(topic, "qbox/pi-uuid/alarm/status")
        self.assertTrue(payload["success"])
        self.assertTrue(payload["alarm_active"])
        self.assertTrue(self.runtime_state.alarm_active)

    def test_alarm_stop_publishes_ack_and_turns_off_hardware(self) -> None:
        self.runtime_state.set_alarm_active(True)

        self.service._handle_alarm_message(
            "qbox/pi-uuid/alarm/control",
            b'{"action":"stop"}',
        )

        self.relay.red_led_off.assert_called_once()
        self.relay.buzzer_off.assert_called_once()
        topic, payload = self.mqtt.publish_json.call_args.args[:2]
        self.assertEqual(topic, "qbox/pi-uuid/alarm/status")
        self.assertTrue(payload["success"])
        self.assertFalse(payload["alarm_active"])
        self.assertFalse(self.runtime_state.alarm_active)

    def test_mqtt_reconnect_publishes_status_ack(self) -> None:
        with patch("app.services.qbox_control.build_system_status", return_value={
            "mqtt_status": "connected",
            "mqtt_connected": True,
            "internal_camera_status": "working",
            "external_camera_status": "not working",
            "qbox_status": "Offline",
            "alarm_active": False,
            "service_status": "running",
            "timestamp": "2026-06-02T12:00:00Z",
        }):
            self.service._handle_mqtt_reconnect_message(
                "qbox/pi-uuid/mqtt/reconnect",
                b"{}",
            )

        self.mqtt.restart_connection.assert_called_once_with(timeout_seconds=15.0)
        topic, payload = self.mqtt.publish_json.call_args.args[:2]
        self.assertEqual(topic, "qbox/pi-uuid/mqtt/status")
        self.assertEqual(payload["device_id"], "pi-uuid")
        self.assertTrue(payload["connected"])
        self.assertIsInstance(payload["last_reconnect"], str)
        self.assertTrue(payload["last_reconnect"])
        self.assertEqual(payload["mqtt_status"], "connected")
        self.assertTrue(payload["mqtt_connected"])
        self.assertTrue(payload["mqtt_online"])
        self.assertEqual(payload["internal_camera_status"], "working")
        self.assertEqual(payload["external_camera_status"], "not working")
        self.assertEqual(payload["qbox_status"], "Offline")
        self.assertEqual(payload["service_status"], "running")
        self.assertEqual(payload["last_health_at"], "2026-06-02T12:00:00Z")
        self.assertEqual(self.runtime_state.last_mqtt_reconnect, payload["last_reconnect"])

    def test_service_restart_rejects_unexpected_service_name(self) -> None:
        restart_service = Mock(return_value=True)
        self.service._restart_service = restart_service

        response = self.service.handle_service_restart({"service": "wrong.service"})

        restart_service.assert_not_called()
        self.assertFalse(response["success"])
        self.assertEqual(response["detail"], "invalid_service")

    def test_service_restart_publishes_ack_for_allowed_service(self) -> None:
        self.service._restart_service = Mock(return_value=True)

        self.service._handle_service_restart_message(
            "qbox/pi-uuid/service/restart",
            b'{"service":"qbox-device.service"}',
        )

        self.service._restart_service.assert_called_once_with("qbox-device.service")
        topic, payload = self.mqtt.publish_json.call_args.args[:2]
        self.assertEqual(topic, "qbox/pi-uuid/service/status")
        self.assertTrue(payload["success"])
        self.assertEqual(payload["service"], "qbox-device.service")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.hardware_agent.mqtt_client import MqttClient
from app.hardware_agent.wifi_responses import build_wifi_connect_failure, build_wifi_connect_success


class MqttServiceRoutingTests(unittest.TestCase):
    def test_state_request_maps_to_state_response(self) -> None:
        self.assertEqual(
            MqttClient._response_topic_for_request(
                "devices/pi-uuid/services/state/request",
                {"command_id": "cmd-1", "service": "state"},
            ),
            "devices/pi-uuid/services/state/response",
        )

    def test_wifi_scan_request_maps_to_wifi_scan_response(self) -> None:
        self.assertEqual(
            MqttClient._response_topic_for_request(
                "devices/pi-uuid/services/wifi.scan/request",
                {"command_id": "cmd-1", "service": "wifi.scan"},
            ),
            "devices/pi-uuid/services/wifi.scan/response",
        )

    def test_wifi_connect_request_maps_to_wifi_connect_response(self) -> None:
        self.assertEqual(
            MqttClient._response_topic_for_request(
                "devices/pi-uuid/services/wifi.connect/request",
                {"command_id": "cmd-1", "service": "wifi.connect"},
            ),
            "devices/pi-uuid/services/wifi.connect/response",
        )

    def test_on_connect_subscribes_to_exact_device_topic_and_legacy_fallback(self) -> None:
        mqtt_client = MqttClient("broker", 1883, "device-1", device_uuid="pi-uuid")
        raw_client = Mock()

        mqtt_client._on_connect(raw_client, None, None, 0)

        raw_client.subscribe.assert_any_call("devices/pi-uuid/services/+/request", qos=1)
        raw_client.subscribe.assert_any_call("devices/+/services/+/request", qos=1)

    def test_mismatched_device_uuid_topic_is_ignored_in_strict_mode(self) -> None:
        mqtt_client = MqttClient("broker", 1883, "device-1", device_uuid="pi-uuid", strict_device_uuid=True)
        mqtt_client.register_command_handler(Mock(return_value={"ok": True}))
        mqtt_client.publish = Mock(return_value=True)

        mqtt_client._on_message(
            None,
            None,
            _message(
                "devices/other-pi/services/state/request",
                {"command_id": "cmd-ignore", "service": "state"},
            ),
        )

        mqtt_client._command_handler.assert_not_called()
        mqtt_client.publish.assert_not_called()

    def test_mismatched_device_uuid_topic_is_accepted_by_default(self) -> None:
        mqtt_client = MqttClient("broker", 1883, "device-1", device_uuid="pi-uuid")
        mqtt_client.register_command_handler(Mock(return_value={"ok": True}))
        mqtt_client.publish = Mock(return_value=True)

        mqtt_client._on_message(
            None,
            None,
            _message(
                "devices/backend-uuid/services/wifi.scan/request",
                {"command_id": "cmd-accept", "service": "wifi.scan"},
            ),
        )

        mqtt_client._command_handler.assert_called_once()
        mqtt_client.publish.assert_called_once_with(
            "devices/backend-uuid/services/wifi.scan/response",
            {
                "command_id": "cmd-accept",
                "service": "wifi.scan",
                "result": {"ok": True},
            },
        )

    def test_command_id_is_echoed_in_service_response(self) -> None:
        mqtt_client = MqttClient("broker", 1883, "device-1", device_uuid="pi-uuid")
        mqtt_client.register_command_handler(Mock(return_value={"wifi_connected": True}))
        mqtt_client.publish = Mock(return_value=True)

        mqtt_client._on_message(
            None,
            None,
            _message(
                "devices/pi-uuid/services/state/request",
                {"command_id": "cmd-state", "service": "state"},
            ),
        )

        mqtt_client.publish.assert_called_once_with(
            "devices/pi-uuid/services/state/response",
            {
                "command_id": "cmd-state",
                "service": "state",
                "result": {"wifi_connected": True},
            },
        )

    def test_topic_service_is_authoritative_for_response(self) -> None:
        mqtt_client = MqttClient("broker", 1883, "device-1", device_uuid="pi-uuid")
        mqtt_client.register_command_handler(Mock(return_value={"networks": []}))
        mqtt_client.publish = Mock(return_value=True)

        mqtt_client._on_message(
            None,
            None,
            _message(
                "devices/pi-uuid/services/wifi.scan/request",
                {"command_id": "cmd-scan", "service": "wifi"},
            ),
        )

        mqtt_client.publish.assert_called_once_with(
            "devices/pi-uuid/services/wifi.scan/response",
            {
                "command_id": "cmd-scan",
                "service": "wifi.scan",
                "result": {"networks": []},
            },
        )

    def test_connect_response_helpers_do_not_return_password(self) -> None:
        success = build_wifi_connect_success(
            "Office",
            {
                "connected_ssid": "Office",
                "ip_address": "192.168.1.20",
                "rssi": -60,
                "signal_strength": 80,
            },
        )
        failure = build_wifi_connect_failure(
            "Office",
            "Authentication failed for Office: wrong or missing WiFi password",
        )

        payload_text = json.dumps({"success": success, "failure": failure})
        self.assertNotIn("PASSWORD", payload_text)
        self.assertNotIn("wrong or missing WiFi password", payload_text)
        self.assertEqual(success["connected_ssid"], "Office")
        self.assertEqual(failure["details"]["reason"], "auth_failed")


def _message(topic: str, payload: dict[str, object]):
    return SimpleNamespace(topic=topic, payload=json.dumps(payload).encode("utf-8"))


if __name__ == "__main__":
    unittest.main()

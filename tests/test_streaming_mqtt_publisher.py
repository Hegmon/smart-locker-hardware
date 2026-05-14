from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.streaming_agent.mqtt_publisher import MQTTPublisher


class StreamingMqttPublisherTests(unittest.TestCase):
    @patch("app.streaming_agent.mqtt_publisher.get_device_id", return_value="device-1")
    def test_disconnect_callback_accepts_paho_v1_signature(self, _device_id) -> None:
        publisher = MQTTPublisher(Mock(), Mock())
        publisher.running = True
        publisher.connected = True

        publisher._on_disconnect(Mock(), None, 7)

        self.assertFalse(publisher.connected)

    @patch("app.streaming_agent.mqtt_publisher.get_device_id", return_value="device-1")
    def test_disconnect_callback_accepts_paho_v2_signature(self, _device_id) -> None:
        publisher = MQTTPublisher(Mock(), Mock())
        publisher.running = True
        publisher.connected = True

        publisher._on_disconnect(Mock(), None, Mock(), 7, None)

        self.assertFalse(publisher.connected)

    @patch("app.streaming_agent.mqtt_publisher.get_device_id", return_value="device-1")
    def test_mqtt_reconnect_restarts_streams_after_previous_connection(self, _device_id) -> None:
        stream_manager = Mock()
        publisher = MQTTPublisher(stream_manager, Mock())
        publisher.running = True
        publisher.connected = False
        publisher._ever_connected = True

        publisher._on_connect(Mock(), None, None, 0)

        stream_manager.restart_all.assert_called_once_with(reason="MQTT reconnected after network loss")

    @patch("app.streaming_agent.mqtt_publisher.get_device_id", return_value="device-1")
    def test_initial_mqtt_connect_does_not_restart_streams(self, _device_id) -> None:
        stream_manager = Mock()
        publisher = MQTTPublisher(stream_manager, Mock())
        publisher.running = True

        publisher._on_connect(Mock(), None, None, 0)

        stream_manager.restart_all.assert_not_called()


if __name__ == "__main__":
    unittest.main()

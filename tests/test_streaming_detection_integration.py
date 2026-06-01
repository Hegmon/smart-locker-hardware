from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.streaming_agent.ffmpeg_builder import (
    QR_FRAME_HEIGHT,
    QR_FRAME_WIDTH,
    INTERNAL_FRAME_FPS,
    build_ffmpeg_command,
)
from app.streaming_agent.frame_buffer import SharedFrameBuffer
from app.streaming_agent.detection.qr_scanner import QRScanResult, QrScanner, parse_qr_value
from app.streaming_agent.streaming_manager import StreamingManager


class StreamingDetectionIntegrationTests(unittest.TestCase):
    @patch("app.streaming_agent.ffmpeg_builder.get_device_id", return_value="device-1")
    def test_internal_stream_command_exports_raw_frame_pipe(self, _device_id) -> None:
        command = build_ffmpeg_command(
            "/dev/video0",
            "internal",
            frame_pipe=True,
            frame_width=640,
            frame_height=480,
        )

        self.assertIn("pipe:1", command)
        self.assertTrue(any("force_original_aspect_ratio=decrease" in item for item in command))
        self.assertTrue(any(f"fps={INTERNAL_FRAME_FPS}" in item for item in command))
        self.assertTrue(any("pad=640:480" in item for item in command))
        self.assertIn("-muxdelay", command)
        self.assertIn("rtsp://69.62.125.223:8554/device-1/internal", command)

    def test_shared_frame_buffer_keeps_latest_frame(self) -> None:
        buffer = SharedFrameBuffer(width=2, height=2, channels=3)
        frame = bytes(range(buffer.frame_size))

        buffer.update(frame)
        latest, sequence, updated_at = buffer.latest()

        self.assertEqual(latest, frame)
        self.assertEqual(sequence, 1)
        self.assertGreater(updated_at, 0)

    @patch("app.streaming_agent.streaming_manager.assign_camera_roles")
    @patch("app.streaming_agent.streaming_manager.CameraControlManager.prepare_for_qr_scan", return_value=True)
    @patch("app.streaming_agent.ffmpeg_builder.get_device_id", return_value="device-1")
    def test_streaming_manager_creates_frame_buffer_for_each_camera(self, _device_id, qr_prepare, roles) -> None:
        roles.return_value = {
            "internal": {"video_device": "/dev/video0"},
            "external": {"video_device": "/dev/video2"},
        }
        manager = StreamingManager()

        manager.initialize()

        self.assertIsNotNone(manager.get_frame_buffer("internal"))
        self.assertIsNotNone(manager.get_frame_buffer("external"))
        self.assertIsNotNone(manager.streams["internal"].frame_buffer)
        self.assertIsNotNone(manager.streams["external"].frame_buffer)
        self.assertEqual(manager.get_frame_buffer("external").width, QR_FRAME_WIDTH)
        self.assertEqual(manager.get_frame_buffer("external").height, QR_FRAME_HEIGHT)
        self.assertEqual(manager.get_frame_buffer("external").channels, 3)
        self.assertIn("pipe:1", manager.streams["external"].ffmpeg_command)
        self.assertIn("pipe:1", manager.streams["internal"].ffmpeg_command)
        qr_prepare.assert_called_once_with("/dev/video2", reason="external camera startup", force=True)

    def test_qr_scanner_rejects_wrong_frame_size_before_reshape(self) -> None:
        buffer = SharedFrameBuffer(width=1280, height=720, channels=3)
        scanner = QrScanner(buffer)

        decoded, qr_seen, metrics = scanner._decode_qr(b"too-short")

        self.assertIsNone(decoded)
        self.assertFalse(qr_seen)
        self.assertEqual(metrics["brightness"], 0.0)

    @patch("app.streaming_agent.detection.qr_scanner.get_shared_mqtt_manager")
    def test_qr_result_publishes_to_shared_mqtt_without_stream_loop(self, mqtt_factory) -> None:
        mqtt = Mock()
        mqtt.device_id = "device-1"
        mqtt.publish_json.return_value = True
        mqtt_factory.return_value = mqtt
        scanner = QrScanner(SharedFrameBuffer(width=1280, height=720, channels=3))

        scanner._publish_result(
            QRScanResult(
                raw_value="token-1",
                payload={"token": "token-1", "locker_id": "device-1"},
                debounce_key="token-1",
            ),
            backend_response={"success": True, "data": {"can_open_locker": True}},
            accepted=True,
        )

        mqtt.publish_json.assert_called_once()
        topic, payload = mqtt.publish_json.call_args.args[:2]
        self.assertEqual(topic, "devices/device-1/qr/scan")
        self.assertTrue(payload["accepted"])

    @patch("app.streaming_agent.detection.qr_scanner.get_device_id", return_value="device-1")
    def test_qr_parser_accepts_raw_token(self, _device_id) -> None:
        payload, debounce_key = parse_qr_value("raw-token-1")

        self.assertEqual(payload["token"], "raw-token-1")
        self.assertEqual(payload["locker_id"], "device-1")
        self.assertEqual(payload["device_id"], "device-1")
        self.assertEqual(debounce_key, "raw-token-1")

    @patch("app.streaming_agent.detection.qr_scanner.get_device_id", return_value="device-1")
    def test_qr_parser_accepts_json_payload(self, _device_id) -> None:
        payload, debounce_key = parse_qr_value(
            '{"qr_code_id":"qr-1","unique_token":"token-1","locker_id":"locker-2","device_id":"pi-2"}'
        )

        self.assertEqual(
            payload,
            {
                "qr_code_id": "qr-1",
                "unique_token": "token-1",
                "locker_id": "locker-2",
                "device_id": "pi-2",
            },
        )
        self.assertEqual(debounce_key, "token-1")

    @patch("app.streaming_agent.detection.qr_scanner.get_device_id", return_value="device-1")
    def test_qr_parser_wraps_full_json_payload_for_backend(self, _device_id) -> None:
        payload, debounce_key = parse_qr_value(
            '{"qr_code_id":"qr-1","unique_token":"token-1","shipment_id":"ship-1","tracking_number":"SHP-1"}'
        )

        self.assertEqual(payload["locker_id"], "device-1")
        self.assertEqual(payload["device_id"], "device-1")
        self.assertEqual(
            payload["qr_payload"],
            {
                "qr_code_id": "qr-1",
                "unique_token": "token-1",
                "shipment_id": "ship-1",
                "tracking_number": "SHP-1",
            },
        )
        self.assertEqual(debounce_key, "token-1")

    @patch("app.streaming_agent.detection.qr_scanner.get_device_id", return_value="device-1")
    def test_qr_parser_preserves_qr_data_payload(self, _device_id) -> None:
        payload, debounce_key = parse_qr_value(
            '{"qr_data":"{\\"qr_code_id\\":\\"qr-1\\",\\"unique_token\\":\\"token-1\\"}"}'
        )

        self.assertEqual(payload["qr_data"], '{"qr_code_id":"qr-1","unique_token":"token-1"}')
        self.assertEqual(payload["locker_id"], "device-1")
        self.assertEqual(payload["device_id"], "device-1")
        self.assertEqual(debounce_key, "token-1")


if __name__ == "__main__":
    unittest.main()

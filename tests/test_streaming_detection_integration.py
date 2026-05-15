from __future__ import annotations

import unittest
from unittest.mock import patch

from app.streaming_agent.ffmpeg_builder import build_ffmpeg_command
from app.streaming_agent.frame_buffer import SharedFrameBuffer
from app.streaming_agent.streaming_manager import StreamingManager


class StreamingDetectionIntegrationTests(unittest.TestCase):
    @patch("app.streaming_agent.ffmpeg_builder.get_device_id", return_value="device-1")
    def test_internal_stream_command_exports_raw_frame_pipe(self, _device_id) -> None:
        command = build_ffmpeg_command("/dev/video0", "internal", frame_pipe=True)

        self.assertIn("pipe:1", command)
        self.assertTrue(any("scale=640:480,format=bgr24" in item for item in command))
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
    @patch("app.streaming_agent.ffmpeg_builder.get_device_id", return_value="device-1")
    def test_streaming_manager_creates_frame_buffer_for_each_camera(self, _device_id, roles) -> None:
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


if __name__ == "__main__":
    unittest.main()

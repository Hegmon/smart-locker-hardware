from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from app.streaming_agent import camera_detector


class CameraDetectorTests(unittest.TestCase):
    def test_detect_usb_cameras_returns_empty_when_v4l2_times_out(self) -> None:
        with patch(
            "app.streaming_agent.camera_detector.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["v4l2-ctl", "--list-devices"], timeout=5),
        ):
            self.assertEqual(camera_detector.detect_usb_cameras(), [])

    def test_detect_usb_cameras_returns_empty_when_v4l2_is_missing(self) -> None:
        with patch(
            "app.streaming_agent.camera_detector.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            self.assertEqual(camera_detector.detect_usb_cameras(), [])


if __name__ == "__main__":
    unittest.main()

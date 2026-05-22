from __future__ import annotations

import unittest

from app.streaming_agent.detection import tamper_detection
from app.streaming_agent.detection.tamper_detection import TamperDetection


class TamperDetectionTests(unittest.TestCase):
    def test_update_tamper_state_turns_led_on_after_confirm_window(self) -> None:
        led = _Led()
        detector = TamperDetection(
            None,
            camera_role="internal",
            led_controller=led,
            tamper_confirm_seconds=0.1,
        )
        detector._tamper_started_at = 0.0

        detector._update_tamper_state(True, "covered")

        self.assertTrue(led.active)
        self.assertEqual(led.role, "internal")

    def test_update_tamper_state_turns_led_off_after_clear_window(self) -> None:
        led = _Led()
        detector = TamperDetection(
            None,
            camera_role="external",
            led_controller=led,
            tamper_clear_seconds=0.1,
        )
        detector._tamper_active = True
        detector._last_tamper_seen_at = 0.0

        detector._update_tamper_state(False, "")

        self.assertFalse(led.active)
        self.assertEqual(led.role, "external")

    def test_update_tamper_state_waits_for_default_clear_timeout(self) -> None:
        led = _Led()
        detector = TamperDetection(
            None,
            camera_role="internal",
            led_controller=led,
        )
        detector._tamper_active = True

        detector._update_tamper_state(False, "")

        self.assertIsNone(led.active)
        self.assertTrue(detector._tamper_active)

    def test_update_tamper_state_can_clear_immediately_when_configured(self) -> None:
        led = _Led()
        detector = TamperDetection(
            None,
            camera_role="internal",
            led_controller=led,
            tamper_clear_seconds=0.0,
        )
        detector._tamper_active = True

        detector._update_tamper_state(False, "")

        self.assertFalse(led.active)
        self.assertEqual(led.role, "internal")

    @unittest.skipIf(tamper_detection.np is None, "numpy unavailable")
    def test_dark_frame_is_tamper(self) -> None:
        buffer = _Buffer()
        detector = TamperDetection(buffer, camera_role="internal")
        detector._baseline_frame_target = 0
        detector._baseline_frames_seen = 1
        detector._baseline_gray = tamper_detection.np.full((120, 160), 120, dtype=tamper_detection.np.float32)
        detector._baseline_brightness = 120.0
        frame = bytes(buffer.frame_size)

        tampered, reason = detector._detect_tamper(frame)

        self.assertTrue(tampered)
        self.assertIn("covered/dark", reason)

    @unittest.skipIf(tamper_detection.np is None, "numpy unavailable")
    def test_bright_frame_is_tamper(self) -> None:
        buffer = _Buffer()
        detector = TamperDetection(buffer, camera_role="external")
        detector._baseline_frame_target = 0
        detector._baseline_frames_seen = 1
        detector._baseline_gray = tamper_detection.np.full((120, 160), 120, dtype=tamper_detection.np.float32)
        detector._baseline_brightness = 120.0
        frame = bytes([255]) * buffer.frame_size

        tampered, reason = detector._detect_tamper(frame)

        self.assertTrue(tampered)
        self.assertIn("covered/bright", reason)

    @unittest.skipIf(tamper_detection.np is None, "numpy unavailable")
    def test_scene_change_is_not_tamper_by_default(self) -> None:
        buffer = _Buffer()
        detector = TamperDetection(buffer, camera_role="external")
        detector._baseline_frame_target = 0
        detector._baseline_frames_seen = 1
        detector._baseline_gray = tamper_detection.np.zeros((120, 160), dtype=tamper_detection.np.float32)
        detector._baseline_brightness = 0.0
        frame = bytes([120, 80, 40]) * buffer.frame_size

        tampered, _ = detector._detect_tamper(frame)

        self.assertFalse(tampered)

    @unittest.skipIf(tamper_detection.np is None, "numpy unavailable")
    def test_dark_startup_frame_calibrates_instead_of_triggering_tamper(self) -> None:
        buffer = _Buffer()
        detector = TamperDetection(buffer, camera_role="internal")
        frame = bytes(buffer.frame_size)

        tampered, _ = detector._detect_tamper(frame)

        self.assertFalse(tampered)


class _Led:
    def __init__(self):
        self.role = None
        self.active = None

    def set_tamper_active(self, camera_role, active):
        self.role = camera_role
        self.active = active


class _Buffer:
    width = 8
    height = 8
    channels = 3
    frame_size = width * height * channels


if __name__ == "__main__":
    unittest.main()

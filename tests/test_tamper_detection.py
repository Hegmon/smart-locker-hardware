from __future__ import annotations

import unittest

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


class _Led:
    def __init__(self):
        self.role = None
        self.active = None

    def set_tamper_active(self, camera_role, active):
        self.role = camera_role
        self.active = active


if __name__ == "__main__":
    unittest.main()

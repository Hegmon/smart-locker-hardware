from __future__ import annotations

import unittest

from app.streaming_agent.detection.person_detector import PersonDetector


class PersonDetectorStateTests(unittest.TestCase):
    def test_update_led_state_turns_off_after_first_clear_frame(self) -> None:
        led = _Led()
        detector = PersonDetector(
            None,
            led_controller=led,
            led_off_delay_seconds=0,
        )
        detector._required_detection_frames = 1
        detector._required_clear_frames = 1

        detector._update_led_state(True, "body_motion")
        detector._update_led_state(False, "")

        self.assertEqual(led.visible_calls, [True, False])
        self.assertFalse(detector._led_visible)

    def test_update_led_state_sends_defensive_off_when_local_state_is_clear(self) -> None:
        led = _Led()
        detector = PersonDetector(
            None,
            led_controller=led,
            led_off_delay_seconds=0,
        )
        detector._required_clear_frames = 1

        detector._update_led_state(False, "")

        self.assertEqual(led.visible_calls, [False])


class _Led:
    def __init__(self):
        self.visible_calls = []

    def set_person_visible(self, visible):
        self.visible_calls.append(visible)


if __name__ == "__main__":
    unittest.main()

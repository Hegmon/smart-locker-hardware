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

        # detectors no longer directly drive relays (state manager is authoritative)
        self.assertEqual(led.visible_calls, [])
        self.assertFalse(detector._led_visible)

    def test_update_led_state_holds_relay_until_clear_timeout(self) -> None:
        led = _Led()
        detector = PersonDetector(
            None,
            led_controller=led,
            led_off_delay_seconds=5.0,
        )
        detector._required_detection_frames = 1
        detector._required_clear_frames = 1

        detector._update_led_state(True, "body_motion")
        detector._update_led_state(False, "")

        # detectors no longer directly drive relays (state manager is authoritative)
        self.assertEqual(led.visible_calls, [])
        self.assertTrue(detector._led_visible)

    def test_update_led_state_sends_defensive_off_when_local_state_is_clear(self) -> None:
        led = _Led()
        detector = PersonDetector(
            None,
            led_controller=led,
            led_off_delay_seconds=0,
        )
        detector._required_clear_frames = 1

        detector._update_led_state(False, "")

        # detectors no longer directly drive relays (state manager is authoritative)
        self.assertEqual(led.visible_calls, [])

    def test_near_object_detection_is_disabled_by_default(self) -> None:
        detector = PersonDetector(None, led_controller=_Led())

        self.assertFalse(detector._near_object_enabled)

    def test_human_score_fuses_partial_signals(self) -> None:
        self.assertGreaterEqual(
            PersonDetector._human_score(
                face_detected=True,
                hand_detected=False,
                person_detected=False,
                motion_detected=True,
            ),
            0.5,
        )


class _Led:
    def __init__(self):
        self.visible_calls = []

    def set_person_visible(self, visible):
        self.visible_calls.append(visible)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import time
import unittest

from app.streaming_agent.detection import person_detector as person_detector_module
from app.streaming_agent.detection.person_detector import PersonDetector


class PersonDetectorStateTests(unittest.TestCase):
    def test_hand_signal_updates_aggregate_human_presence(self) -> None:
        manager = _StateManager()
        detector = PersonDetector(
            None,
            led_controller=_Led(),
            detection_state_manager=manager,
        )
        detector._hand_trigger_frames = 1

        detector._update_led_state(
            False,
            "hand area=0.020",
            hand_detected=True,
            human_score=0.9,
        )

        self.assertTrue(manager.calls[-1]["hand_detected"])
        self.assertTrue(manager.calls[-1]["person_detected"])

    def test_person_only_updates_state_manager_without_human_score_latch(self) -> None:
        manager = _StateManager()
        detector = PersonDetector(
            None,
            led_controller=_Led(),
            detection_state_manager=manager,
        )
        detector._required_detection_frames = 1
        detector._required_clear_frames = 1

        detector._update_led_state(
            False,
            "person_model",
            person_detected=True,
            human_score=0.5,
        )

        self.assertTrue(manager.calls[-1]["person_detected"])

    def test_person_model_requires_current_score_and_smoothed_score(self) -> None:
        if person_detector_module.np is None:
            self.skipTest("numpy unavailable")
        detector = PersonDetector(None, led_controller=_Led())
        detector.confidence_threshold = 0.7
        detector._person_confidence_ema = 0.9

        scores = person_detector_module.np.array([0.55], dtype=person_detector_module.np.float32)
        classes = person_detector_module.np.array([0], dtype=person_detector_module.np.float32)
        boxes = person_detector_module.np.array([[0.1, 0.1, 0.7, 0.7]], dtype=person_detector_module.np.float32)

        self.assertEqual(detector._model_person_detected(scores, classes, boxes), (False, ""))

    def test_update_led_state_turns_off_after_clear_timeout(self) -> None:
        led = _Led()
        detector = PersonDetector(
            None,
            led_controller=led,
            led_off_delay_seconds=0,
        )
        detector._required_detection_frames = 1
        detector._required_clear_frames = 1
        detector._clear_seconds = 0
        detector._presence_timeout_seconds = 0

        detector._update_led_state(True, "person_model")
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

        detector._update_led_state(True, "person_model")
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
        self.assertEqual(
            PersonDetector._human_score(
                face_detected=True,
                hand_detected=False,
                person_detected=False,
            ),
            0.7,
        )

    def test_body_signal_updates_aggregate_human_presence(self) -> None:
        manager = _StateManager()
        detector = PersonDetector(
            None,
            led_controller=_Led(),
            detection_state_manager=manager,
        )
        detector._body_trigger_frames = 1

        detector._update_led_state(
            False,
            "upper_body area=0.050",
            body_detected=True,
            human_score=0.8,
        )

        self.assertTrue(detector._body_active)
        self.assertTrue(manager.calls[-1]["person_detected"])

    def test_human_score_counts_body_signal(self) -> None:
        self.assertEqual(
            PersonDetector._human_score(
                face_detected=False,
                hand_detected=False,
                person_detected=False,
                body_detected=True,
            ),
            0.65,
        )

    def test_person_model_hold_allows_stable_presence_below_primary_threshold(self) -> None:
        if person_detector_module.np is None:
            self.skipTest("numpy unavailable")
        detector = PersonDetector(None, led_controller=_Led())
        detector.confidence_threshold = 0.7
        detector._confidence_hold_threshold = 0.45
        detector._model_hold_confidence_threshold = 0.45
        detector._person_active = True
        detector._person_confidence_ema = 0.72

        scores = person_detector_module.np.array([0.5], dtype=person_detector_module.np.float32)
        classes = person_detector_module.np.array([0], dtype=person_detector_module.np.float32)
        boxes = person_detector_module.np.array([[0.1, 0.1, 0.7, 0.7]], dtype=person_detector_module.np.float32)

        detected, reason = detector._model_person_detected(scores, classes, boxes)

        self.assertTrue(detected)
        self.assertIn("person_model_hold", reason)

    def test_hand_signal_needs_close_range_when_unsupported(self) -> None:
        detector = PersonDetector(None, led_controller=_Led())
        detector._hand_standalone_min_area = 0.08

        self.assertFalse(
            detector._hand_signal_is_valid(
                True,
                0.035,
                has_supporting_signal=False,
            )
        )
        self.assertTrue(
            detector._hand_signal_is_valid(
                True,
                0.10,
                has_supporting_signal=False,
            )
        )

    def test_small_hand_signal_counts_with_supporting_human_signal(self) -> None:
        detector = PersonDetector(None, led_controller=_Led())
        detector._hand_standalone_min_area = 0.08

        self.assertTrue(
            detector._hand_signal_is_valid(
                True,
                0.035,
                has_supporting_signal=True,
            )
        )

    def test_human_presence_stays_active_through_temporary_missed_frames(self) -> None:
        manager = _StateManager()
        detector = PersonDetector(
            None,
            led_controller=_Led(),
            detection_state_manager=manager,
            led_off_delay_seconds=0.5,
        )
        detector._required_detection_frames = 1
        detector._required_clear_frames = 3
        detector._clear_seconds = 0.5

        detector._update_led_state(True, "person_model", person_detected=True, human_score=0.9)
        detector._update_led_state(False, "")
        detector._update_led_state(False, "")

        self.assertTrue(detector._led_visible)
        self.assertTrue(manager.calls[-1]["person_detected"])

    def test_human_presence_clears_after_missed_frames_and_timeout(self) -> None:
        manager = _StateManager()
        detector = PersonDetector(
            None,
            led_controller=_Led(),
            detection_state_manager=manager,
            led_off_delay_seconds=0,
        )
        detector._required_detection_frames = 1
        detector._required_clear_frames = 1
        detector._clear_seconds = 0.01
        detector._presence_timeout_seconds = 0.01

        detector._update_led_state(True, "person_model", person_detected=True, human_score=0.9)
        time.sleep(0.02)
        detector._update_led_state(False, "")

        self.assertFalse(detector._led_visible)
        self.assertFalse(manager.calls[-1]["person_detected"])

    def test_internal_presence_holds_for_configured_absence_timeout(self) -> None:
        manager = _StateManager()
        detector = PersonDetector(
            None,
            led_controller=_Led(),
            detection_state_manager=manager,
        )
        detector._required_detection_frames = 1
        detector._required_clear_frames = 1
        detector._clear_seconds = 0
        detector._presence_timeout_seconds = 0.05

        detector._update_led_state(True, "person_model", person_detected=True, human_score=0.9)
        detector._update_led_state(False, "")

        self.assertTrue(detector._led_visible)
        self.assertTrue(manager.calls[-1]["person_detected"])

        time.sleep(0.06)
        detector._update_led_state(False, "")

        self.assertFalse(detector._led_visible)
        self.assertFalse(manager.calls[-1]["person_detected"])
        self.assertIn("internal", manager.timeout_checks)

    def test_internal_presence_reappearing_resets_absence_timeout(self) -> None:
        manager = _StateManager()
        detector = PersonDetector(
            None,
            led_controller=_Led(),
            detection_state_manager=manager,
        )
        detector._required_detection_frames = 1
        detector._required_clear_frames = 1
        detector._clear_seconds = 0
        detector._presence_timeout_seconds = 0.08

        detector._update_led_state(True, "person_model", person_detected=True, human_score=0.9)
        time.sleep(0.04)
        detector._update_led_state(False, "")
        self.assertTrue(detector._led_visible)

        detector._update_led_state(True, "person_model", person_detected=True, human_score=0.9)
        time.sleep(0.04)
        detector._update_led_state(False, "")

        self.assertTrue(detector._led_visible)
        self.assertTrue(manager.calls[-1]["person_detected"])

    def test_stale_frame_path_clears_after_internal_presence_timeout(self) -> None:
        manager = _StateManager()
        detector = PersonDetector(
            None,
            led_controller=_Led(),
            detection_state_manager=manager,
        )
        detector._led_visible = True
        detector._person_active = True
        detector._presence_timeout_seconds = 0.05
        detector._last_person_detected_time = time.monotonic() - 0.06

        detector._clear_stale_led_state()

        self.assertEqual(manager.clear_calls, ["internal"])
        self.assertIn("internal", manager.timeout_checks)
        self.assertFalse(detector._person_active)

    def test_stale_internal_detection_clears_state_manager(self) -> None:
        manager = _StateManager()
        detector = PersonDetector(
            None,
            led_controller=_Led(),
            detection_state_manager=manager,
        )
        detector._led_visible = True
        detector._person_active = True
        detector._last_person_seen_at = time.monotonic() - 1.0
        detector._presence_timeout_seconds = 0.05
        detector._stale_clear_seconds = 0.05

        detector._clear_stale_led_state()

        self.assertEqual(manager.clear_calls, ["internal"])
        self.assertFalse(detector._person_active)


class _Led:
    """Legacy mock kept only for detector constructor compatibility in tests.
    Detectors no longer call any relay methods directly."""
    def __init__(self) -> None:
        self.visible_calls = []


class _StateManager:
    def __init__(self) -> None:
        self.calls = []
        self.clear_calls = []
        self.timeout_checks = []

    def update_presence(self, camera_role, **kwargs):
        self.calls.append({"camera_role": camera_role, **kwargs})

    def clear_presence(self, camera_role):
        self.clear_calls.append(camera_role)

    def check_timeouts(self):
        self.timeout_checks.append("internal")


if __name__ == "__main__":
    unittest.main()

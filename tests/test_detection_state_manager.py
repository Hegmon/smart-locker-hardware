from __future__ import annotations

import time
import unittest

from app.streaming_agent.detection.state_manager import DetectionStateManager


class DetectionStateManagerTests(unittest.TestCase):
    def test_internal_person_holds_relay_1_through_missed_frames(self) -> None:
        relay = _Relay()
        manager = DetectionStateManager(relay, detection_hold_seconds=0.2, tamper_hold_seconds=0.2)

        manager.update_presence("internal", person_detected=True, reason="person")
        manager.update_presence("internal", person_detected=False, motion_detected=False)

        self.assertEqual(relay.person_calls[-1], True)
        self.assertTrue(manager.camera_state["internal"]["person_detected"])

        time.sleep(0.25)
        manager.check_timeouts()

        self.assertEqual(relay.person_calls[-1], False)
        self.assertFalse(manager.camera_state["internal"]["person_detected"])

    def test_any_camera_tamper_holds_relay_4(self) -> None:
        relay = _Relay()
        manager = DetectionStateManager(relay, detection_hold_seconds=0.2, tamper_hold_seconds=0.2)

        manager.update_tamper("external", tamper_detected=True, reason="covered")
        manager.update_tamper("external", tamper_detected=False)

        self.assertEqual(relay.tamper_calls[-1], ("any", True))
        self.assertTrue(manager.camera_state["external"]["tamper_detected"])

        time.sleep(0.25)
        manager.check_timeouts()

        self.assertEqual(relay.tamper_calls[-1], ("any", False))
        self.assertFalse(manager.camera_state["external"]["tamper_detected"])


class _Relay:
    def __init__(self):
        self.person_calls = []
        self.tamper_calls = []

    def set_person_visible(self, visible):
        self.person_calls.append(visible)

    def set_tamper_active(self, camera_role, active):
        self.tamper_calls.append((camera_role, active))


if __name__ == "__main__":
    unittest.main()

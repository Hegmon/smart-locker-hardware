from __future__ import annotations

import time
import unittest

from app.streaming_agent.detection.state_manager import DetectionStateManager


class DetectionStateManagerTests(unittest.TestCase):
    def test_person_event_synchronizes_both_relays_through_missed_frames(self) -> None:
        relay = _Relay()
        manager = DetectionStateManager(relay, security_hold_seconds=0.2)

        manager.update_presence("internal", person_detected=True, reason="person")
        manager.update_presence("internal", person_detected=False, motion_detected=False)

        self.assertEqual(relay.security_calls[-1], True)
        self.assertTrue(manager.camera_state["internal"]["person_detected"])

        time.sleep(0.25)
        manager.check_timeouts()

        self.assertEqual(relay.security_calls[-1], False)
        self.assertFalse(manager.camera_state["internal"]["person_detected"])

    def test_any_camera_tamper_synchronizes_both_relays(self) -> None:
        relay = _Relay()
        manager = DetectionStateManager(relay, security_hold_seconds=0.2)

        manager.update_tamper("external", tamper_detected=True, reason="covered")
        manager.update_tamper("external", tamper_detected=False)

        self.assertEqual(relay.security_calls[-1], True)
        self.assertTrue(manager.camera_state["external"]["tamper_detected"])

        time.sleep(0.25)
        manager.check_timeouts()

        self.assertEqual(relay.security_calls[-1], False)
        self.assertFalse(manager.camera_state["external"]["tamper_detected"])

    def test_face_alone_is_security_event(self) -> None:
        relay = _Relay()
        manager = DetectionStateManager(relay, security_hold_seconds=0.2)

        manager.update_presence("internal", face_detected=True, human_score=0.4, reason="face")

        self.assertEqual(relay.security_calls[-1], True)
        self.assertTrue(manager.camera_state["internal"]["face_detected"])


class _Relay:
    def __init__(self):
        self.security_calls = []

    def set_security_relays(self, active):
        self.security_calls.append(active)


if __name__ == "__main__":
    unittest.main()

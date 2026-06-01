from __future__ import annotations

import time
import unittest

from app.streaming_agent.config.runtime import (
    DetectionEventConfig,
    PersonDetectionConfig,
    RelayConfig,
    StreamingAgentRuntimeConfig,
    TamperDetectionConfig,
)
from app.streaming_agent.detection.state_manager import DetectionStateManager


class DetectionStateManagerTests(unittest.TestCase):
    def test_person_event_synchronizes_both_relays_through_missed_frames(self) -> None:
        relay = _Relay()
        manager = DetectionStateManager(relay, security_hold_seconds=0.2)

        manager.update_presence("internal", person_detected=True, reason="person")
        self.assertTrue(manager.camera_state["internal"]["person_detected"])
        time.sleep(0.12)
        self.assertEqual(relay.security_calls[-1], True)

        manager.update_presence("internal", person_detected=False)

        time.sleep(0.25)
        manager.check_timeouts()

        self.assertEqual(relay.security_calls[-1], False)
        self.assertFalse(manager.camera_state["internal"]["person_detected"])

    def test_any_camera_tamper_synchronizes_both_relays(self) -> None:
        relay = _Relay()
        manager = DetectionStateManager(relay, security_hold_seconds=0.2)

        manager.update_tamper("external", tamper_detected=True, reason="covered")
        self.assertTrue(manager.camera_state["external"]["tamper_detected"])
        time.sleep(0.12)
        self.assertEqual(relay.security_calls[-1], True)

        manager.update_tamper("external", tamper_detected=False)

        time.sleep(0.25)
        manager.check_timeouts()

        self.assertEqual(relay.security_calls[-1], False)
        self.assertFalse(manager.camera_state["external"]["tamper_detected"])

    def test_face_alone_updates_aggregate_human_presence_and_triggers_security_relays(self) -> None:
        relay = _Relay()
        manager = DetectionStateManager(relay, security_hold_seconds=0.2)

        manager.update_presence("internal", face_detected=True, person_detected=True, human_score=0.7, reason="face")

        self.assertTrue(manager.camera_state["internal"]["face_detected"])
        time.sleep(0.05)

        self.assertEqual(relay.security_calls[-1], True)

    def test_hand_aggregate_presence_triggers_security_relays(self) -> None:
        relay = _Relay()
        manager = DetectionStateManager(relay, security_hold_seconds=0.2)

        manager.update_presence("internal", hand_detected=True, person_detected=True, human_score=0.7, reason="hand")
        time.sleep(0.05)

        self.assertTrue(manager.camera_state["internal"]["hand_detected"])
        self.assertTrue(manager.camera_state["internal"]["person_detected"])
        self.assertEqual(relay.security_calls[-1], True)

    def test_external_person_does_not_trigger_security_relays(self) -> None:
        relay = _Relay()
        manager = DetectionStateManager(relay, security_hold_seconds=0.2)

        manager.update_presence("external", person_detected=True, human_score=0.9, reason="person")
        time.sleep(0.05)

        self.assertEqual(relay.security_calls, [])

    def test_internal_person_source_refreshes_and_expires_if_updates_stop(self) -> None:
        relay = _Relay()
        manager = DetectionStateManager(
            relay,
            security_hold_seconds=0,
            runtime_config=_runtime_config(source_ttl=0.30, refresh_seconds=0.03),
        )

        manager.update_presence("internal", person_detected=True, human_score=0.9, reason="person")
        self.assertEqual(relay.security_calls[-1], True)
        time.sleep(0.08)
        manager.update_presence("internal", person_detected=True, human_score=0.9, reason="person")
        time.sleep(0.12)

        self.assertTrue(relay.security_active)

        time.sleep(0.35)

        self.assertEqual(relay.security_calls[-1], False)
        self.assertFalse(relay.security_active)


class _Relay:
    def __init__(self):
        self.security_calls = []
        self.person_calls = []
        self.tamper_calls = []
        self.security_active = False

    def set_security_relays(self, active):
        self.security_calls.append(active)
        self.security_active = bool(active)

    def is_security_relays_on(self):
        return self.security_active

    def force_security_relays_off(self):
        self.security_calls.append(False)
        self.security_active = False

    def set_person_visible(self, active):
        self.person_calls.append(active)

    def set_tamper_active(self, camera_role, active):
        self.tamper_calls.append(active)


def _runtime_config(source_ttl=5.0, refresh_seconds=0.25):
    return StreamingAgentRuntimeConfig(
        relay=RelayConfig(
            timeout_seconds=0.0,
            detection_debounce_seconds=0.0,
            active_source_ttl_seconds=source_ttl,
            retry_count=3,
            retry_delay_seconds=0.0,
            poll_interval_seconds=0.02,
            stale_on_failsafe_seconds=10.0,
            state_log_interval_seconds=10.0,
        ),
        event=DetectionEventConfig(cooldown_seconds=refresh_seconds),
        person=PersonDetectionConfig(confidence_threshold=0.75),
        tamper=TamperDetectionConfig(
            confirm_seconds=0.2,
            clear_seconds=0.0,
            dark_brightness_threshold=28.0,
            bright_brightness_threshold=242.0,
            blur_threshold=12.0,
            edge_density_threshold=0.005,
            large_change_threshold=0.58,
            scene_change_enabled=False,
        ),
    )

if __name__ == "__main__":
    unittest.main()

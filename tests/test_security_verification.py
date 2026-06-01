from __future__ import annotations

import time
import unittest
from dataclasses import replace

from app.streaming_agent.config.runtime import StreamingAgentRuntimeConfig
from app.streaming_agent.detection.state_manager import DetectionStateManager


class _RecordingRelay:
    def __init__(self):
        self.security_calls = []
        self.fail_next_write = False

    def set_security_relays(self, active):
        if self.fail_next_write:
            self.fail_next_write = False
            raise RuntimeError("simulated relay failure")
        self.security_calls.append(active)

    def is_security_relays_on(self):
        return bool(self.security_calls and self.security_calls[-1])

    def force_security_relays_off(self):
        self.security_calls.append(False)

class SecurityVerificationTests(unittest.TestCase):
    def test_relay_manager_turns_off_after_timeout(self) -> None:
        relay = _RecordingRelay()
        manager = DetectionStateManager(relay, security_hold_seconds=0.2)

        manager.update_presence("internal", person_detected=True, human_score=0.9, reason="person")
        time.sleep(0.05)
        self.assertEqual(relay.security_calls[-1], True)
        manager.update_presence("internal", person_detected=False, human_score=0.0, reason="clear")
        time.sleep(0.35)

        self.assertEqual(relay.security_calls[-1], False)

    def test_new_person_detection_cancels_pending_off_deadline(self) -> None:
        relay = _RecordingRelay()
        manager = DetectionStateManager(relay, security_hold_seconds=0.2)

        manager.update_presence("internal", person_detected=True, human_score=0.9, reason="person")
        manager.update_presence("internal", person_detected=False, human_score=0.0, reason="clear")

        time.sleep(0.05)
        manager.update_presence("internal", person_detected=True, human_score=0.9, reason="person")
        time.sleep(0.07)

        self.assertEqual(relay.security_calls[-1], True)

    def test_continuous_active_detection_stays_on_until_clear(self) -> None:
        relay = _RecordingRelay()
        runtime_config = StreamingAgentRuntimeConfig.from_env()
        runtime_config = replace(
            runtime_config,
            relay=replace(runtime_config.relay, detection_debounce_seconds=0.1),
        )
        manager = DetectionStateManager(
            relay,
            security_hold_seconds=0.5,
            runtime_config=runtime_config,
        )

        manager.update_presence("internal", person_detected=True, human_score=0.9, reason="person")
        time.sleep(0.12)
        manager.update_presence("internal", person_detected=True, human_score=0.9, reason="person")
        time.sleep(0.12)
        manager.update_presence("internal", person_detected=True, human_score=0.9, reason="person")
        time.sleep(0.12)

        self.assertEqual(relay.security_calls[-1], True)
        self.assertEqual(relay.security_calls.count(True), 1)

        manager.update_presence("internal", person_detected=False, human_score=0.0, reason="clear")
        time.sleep(0.55)

        self.assertEqual(relay.security_calls[-1], False)

    def test_duplicate_detected_events_do_not_spam_relay_on(self) -> None:
        relay = _RecordingRelay()
        manager = DetectionStateManager(relay, security_hold_seconds=0.2)

        manager.update_presence("internal", person_detected=True, human_score=0.9, reason="person")
        manager.update_presence("internal", person_detected=True, human_score=0.9, reason="person")
        manager.update_presence("internal", person_detected=True, human_score=0.9, reason="person")
        time.sleep(0.05)

        self.assertEqual(relay.security_calls.count(True), 1)

    def test_worker_recovers_after_on_command_exception(self) -> None:
        relay = _RecordingRelay()
        relay.fail_next_write = True
        manager = DetectionStateManager(relay, security_hold_seconds=0.2)

        manager.update_presence("internal", person_detected=True, human_score=0.9, reason="person")
        time.sleep(0.3)

        self.assertEqual(relay.security_calls[-1], True)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import time
import unittest

from app.streaming_agent.detection.state_manager import DetectionStateManager


class _StuckRelay:
    def __init__(self):
        self.security_calls = []
        self.force_called = False

    def set_security_relays(self, active):
        # record attempt; but hardware remains stuck
        self.security_calls.append(active)
        self._last = active

    def is_security_relays_on(self):
        # simulate hardware stuck ON regardless of commands
        return True

    def force_security_relays_off(self):
        self.force_called = True


class SecurityVerificationTests(unittest.TestCase):
    def test_verifier_forces_off_on_mismatch(self) -> None:
        relay = _StuckRelay()
        # short hold so test completes quickly
        manager = DetectionStateManager(relay, security_hold_seconds=0.1)

        # give verifier thread a moment to run its immediate check
        time.sleep(0.2)

        # verifier should detect mismatch (computed=False but hardware reports ON)
        # and call the force method
        self.assertTrue(relay.force_called)


if __name__ == "__main__":
    unittest.main()

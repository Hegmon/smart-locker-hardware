from __future__ import annotations

import time
import unittest

from app.streaming_agent.detection import tamper_detection
from app.streaming_agent.detection.tamper_detection import (
    TAMPER_STATE_ACTIVE,
    TAMPER_STATE_CANDIDATE,
    TAMPER_STATE_CLEARING,
    TAMPER_STATE_IDLE,
    TamperDetection,
)


class TamperDetectionTests(unittest.TestCase):
    def test_update_tamper_state_publishes_once_on_active_transition(self) -> None:
        manager = _StateManager()
        detector = TamperDetection(
            None,
            camera_role="internal",
            led_controller=_Led(),
            detection_state_manager=manager,
            tamper_confirm_seconds=0.1,
        )
        detector._required_tamper_frames = 1
        detector._update_tamper_state(True, "covered")
        self.assertEqual(detector._tamper_state, TAMPER_STATE_CANDIDATE)
        self.assertEqual(manager.calls, [])

        time.sleep(0.11)
        detector._update_tamper_state(True, "covered")
        detector._update_tamper_state(True, "covered")

        self.assertEqual(detector._tamper_state, TAMPER_STATE_ACTIVE)
        self.assertEqual(manager.calls[0], ("internal", True, "covered"))

    def test_update_tamper_state_enters_clearing_then_idles(self) -> None:
        manager = _StateManager()
        detector = TamperDetection(
            None,
            camera_role="external",
            led_controller=_Led(),
            detection_state_manager=manager,
            tamper_clear_seconds=0.1,
        )
        detector._tamper_state = TAMPER_STATE_ACTIVE
        detector._required_clear_frames = 1

        detector._update_tamper_state(False, "")
        self.assertEqual(detector._tamper_state, TAMPER_STATE_CLEARING)
        self.assertEqual(manager.calls, [])

        time.sleep(0.11)
        detector._update_tamper_state(False, "")

        self.assertEqual(detector._tamper_state, TAMPER_STATE_IDLE)
        self.assertEqual(manager.calls, [("external", False, "")])

    def test_update_tamper_state_waits_for_default_clear_timeout(self) -> None:
        detector = TamperDetection(
            None,
            camera_role="internal",
            led_controller=_Led(),
        )
        detector._tamper_state = TAMPER_STATE_ACTIVE
        detector._required_clear_frames = 1

        detector._update_tamper_state(False, "")

        self.assertEqual(detector._tamper_state, TAMPER_STATE_CLEARING)

    def test_update_tamper_state_can_clear_immediately_when_configured(self) -> None:
        manager = _StateManager()
        detector = TamperDetection(
            None,
            camera_role="internal",
            led_controller=_Led(),
            detection_state_manager=manager,
            tamper_clear_seconds=0.0,
        )
        detector._tamper_state = TAMPER_STATE_ACTIVE
        detector._required_clear_frames = 1

        detector._update_tamper_state(False, "")
        detector._update_tamper_state(False, "")

        self.assertEqual(detector._tamper_state, TAMPER_STATE_IDLE)
        self.assertEqual(manager.calls, [("internal", False, "")])

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
    """Minimal legacy mock so existing tamper tests can assert 'no direct call happened'."""
    def __init__(self):
        self.role = None
        self.active = None


class _Buffer:
    width = 8
    height = 8
    channels = 3
    frame_size = width * height * channels


class _StateManager:
    def __init__(self) -> None:
        self.calls = []

    def update_tamper(self, camera_role, *, tamper_detected=False, reason=""):
        self.calls.append((camera_role, tamper_detected, reason))

    def clear_tamper(self, camera_role):
        self.calls.append((camera_role, False, ""))


if __name__ == "__main__":
    unittest.main()

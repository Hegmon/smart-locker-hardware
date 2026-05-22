from __future__ import annotations

import sys
import time
import types
import unittest
from unittest.mock import patch

from app.streaming_agent.gpio.relay_controller import RelayController


class RelayControllerTests(unittest.TestCase):
    def test_start_initializes_bcm_pins_in_safe_default_state(self) -> None:
        gpio = _FakeGPIO()
        controller = RelayController(active_low=True)

        with patch.dict(sys.modules, {"RPi": types.SimpleNamespace(GPIO=gpio), "RPi.GPIO": gpio}):
            controller.start()

        self.assertEqual(gpio.mode, gpio.BCM)
        self.assertEqual(gpio.setup_calls, [(21, gpio.OUT, gpio.HIGH), (20, gpio.OUT, gpio.HIGH), (16, gpio.OUT, gpio.HIGH), (12, gpio.OUT, gpio.HIGH)])
        self.assertEqual(gpio.outputs[21], gpio.HIGH)
        self.assertEqual(gpio.outputs[20], gpio.HIGH)
        self.assertEqual(gpio.outputs[16], gpio.HIGH)
        self.assertEqual(gpio.outputs[12], gpio.HIGH)

    def test_person_detection_drives_relay_1_only(self) -> None:
        gpio = _FakeGPIO()
        controller = RelayController(active_low=True)
        with patch.dict(sys.modules, {"RPi": types.SimpleNamespace(GPIO=gpio), "RPi.GPIO": gpio}):
            controller.start()
            controller.set_person_visible(True)

        self.assertEqual(gpio.outputs[21], gpio.LOW)
        self.assertEqual(gpio.outputs[12], gpio.HIGH)
        self.assertEqual(gpio.outputs[20], gpio.HIGH)
        self.assertEqual(gpio.outputs[16], gpio.HIGH)

    def test_person_detection_clears_red_led_and_buzzer_without_timer(self) -> None:
        gpio = _FakeGPIO()
        controller = RelayController(active_low=True)
        with patch.dict(sys.modules, {"RPi": types.SimpleNamespace(GPIO=gpio), "RPi.GPIO": gpio}):
            controller.start()
            controller.set_person_visible(True)
            controller.set_person_visible(False)

        self.assertEqual(gpio.outputs[21], gpio.HIGH)
        self.assertEqual(gpio.outputs[12], gpio.HIGH)
        self.assertEqual(controller._alert_threads, set())

    def test_tamper_detection_drives_and_clears_relay_4_only(self) -> None:
        gpio = _FakeGPIO()
        controller = RelayController(active_low=True)
        with patch.dict(sys.modules, {"RPi": types.SimpleNamespace(GPIO=gpio), "RPi.GPIO": gpio}):
            controller.start()
            controller.set_tamper_active("internal", True)
            self.assertEqual(gpio.outputs[21], gpio.HIGH)
            self.assertEqual(gpio.outputs[12], gpio.LOW)
            controller.set_tamper_active("internal", False)

        self.assertEqual(gpio.outputs[21], gpio.HIGH)
        self.assertEqual(gpio.outputs[12], gpio.HIGH)
        self.assertEqual(controller._alert_threads, set())

    def test_security_event_synchronizes_relay_1_and_relay_4(self) -> None:
        gpio = _FakeGPIO()
        controller = RelayController(active_low=True)
        with patch.dict(sys.modules, {"RPi": types.SimpleNamespace(GPIO=gpio), "RPi.GPIO": gpio}):
            controller.start()
            controller.set_security_relays(True)
            self.assertEqual(gpio.outputs[21], gpio.LOW)
            self.assertEqual(gpio.outputs[12], gpio.LOW)
            controller.set_security_relays(False)

        self.assertEqual(gpio.outputs[21], gpio.HIGH)
        self.assertEqual(gpio.outputs[12], gpio.HIGH)

    def test_detection_ttl_turns_relay_off_if_detector_stops_refreshing(self) -> None:
        gpio = _FakeGPIO()
        controller = RelayController(active_low=True)
        with patch.dict(sys.modules, {"RPi": types.SimpleNamespace(GPIO=gpio), "RPi.GPIO": gpio}):
            with patch.dict("os.environ", {"DETECTION_RELAY_SOURCE_TTL_SECONDS": "0.05"}):
                controller.start()
                controller.set_person_visible(True)
                self.assertEqual(gpio.outputs[21], gpio.LOW)
                time.sleep(0.15)

        self.assertEqual(gpio.outputs[21], gpio.HIGH)
        self.assertNotIn("person", controller._red_sources)

    def test_lock_locker_uses_inactive_relay_state(self) -> None:
        gpio = _FakeGPIO()
        controller = RelayController(active_low=True)
        with patch.dict(sys.modules, {"RPi": types.SimpleNamespace(GPIO=gpio), "RPi.GPIO": gpio}):
            controller.start()
            controller.unlock_locker()
            controller.lock_locker()

        self.assertEqual(gpio.outputs[16], gpio.HIGH)


class _FakeGPIO:
    BCM = "BCM"
    OUT = "OUT"
    HIGH = 1
    LOW = 0

    def __init__(self):
        self.mode = None
        self.setup_calls = []
        self.outputs = {}

    def setmode(self, mode):
        self.mode = mode

    def setwarnings(self, enabled):
        self.warnings = enabled

    def setup(self, pin, direction, initial=None):
        self.setup_calls.append((pin, direction, initial))
        self.outputs[pin] = initial

    def output(self, pin, state):
        self.outputs[pin] = state

    def cleanup(self, pins=None):
        self.cleaned = pins


if __name__ == "__main__":
    unittest.main()

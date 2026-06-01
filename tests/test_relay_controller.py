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

    def test_security_event_drives_both_relays_together(self) -> None:
        """Modern centralized path: DetectionStateManager calls set_security_relays only."""
        gpio = _FakeGPIO()
        controller = RelayController(active_low=True)
        with patch.dict(sys.modules, {"RPi": types.SimpleNamespace(GPIO=gpio), "RPi.GPIO": gpio}):
            controller.start()
            controller.set_security_relays(True)

        self.assertEqual(gpio.outputs[21], gpio.LOW)   # Red LED (Relay 1)
        self.assertEqual(gpio.outputs[12], gpio.LOW)   # Buzzer (Relay 4)

        controller.set_security_relays(False)
        self.assertEqual(gpio.outputs[21], gpio.HIGH)
        self.assertEqual(gpio.outputs[12], gpio.HIGH)

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

    def test_force_security_relays_off_preserves_non_security_alert_sources(self) -> None:
        gpio = _FakeGPIO()
        controller = RelayController(active_low=True)
        controller.allow_shared_alert_outputs = True
        with patch.dict(sys.modules, {"RPi": types.SimpleNamespace(GPIO=gpio), "RPi.GPIO": gpio}):
            controller.start()
            controller.trigger_alert("qr_failure", duration_seconds=1.0)
            controller.set_security_relays(True)
            controller.force_security_relays_off()

        self.assertEqual(gpio.outputs[21], gpio.LOW)
        self.assertEqual(gpio.outputs[12], gpio.LOW)
        self.assertNotIn("security_event", controller._red_sources)
        self.assertIn("qr_failure", controller._red_sources)

    def test_lock_locker_uses_inactive_relay_state(self) -> None:
        gpio = _FakeGPIO()
        controller = RelayController(active_low=True)
        with patch.dict(sys.modules, {"RPi": types.SimpleNamespace(GPIO=gpio), "RPi.GPIO": gpio}):
            controller.start()
            controller.unlock_locker()
            controller.lock_locker()

        self.assertEqual(gpio.outputs[16], gpio.HIGH)

    def test_gpio_write_retries_until_readback_matches(self) -> None:
        gpio = _FlakyReadbackGPIO()
        controller = RelayController(active_low=True)
        with patch.dict(sys.modules, {"RPi": types.SimpleNamespace(GPIO=gpio), "RPi.GPIO": gpio}):
            controller.start()
            controller.set_security_relays(True)

        self.assertGreaterEqual(gpio.output_calls[21], 2)
        self.assertEqual(gpio.outputs[21], gpio.LOW)


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

    def input(self, pin):
        return self.outputs.get(pin, self.HIGH)

    def cleanup(self, pins=None):
        self.cleaned = pins


class _FlakyReadbackGPIO(_FakeGPIO):
    def __init__(self):
        super().__init__()
        self.output_calls = {}
        self._input_mismatch_once = {21: True, 12: True}

    def output(self, pin, state):
        super().output(pin, state)
        self.output_calls[pin] = self.output_calls.get(pin, 0) + 1

    def input(self, pin):
        if self._input_mismatch_once.get(pin):
            self._input_mismatch_once[pin] = False
            return self.HIGH
        return super().input(pin)


if __name__ == "__main__":
    unittest.main()

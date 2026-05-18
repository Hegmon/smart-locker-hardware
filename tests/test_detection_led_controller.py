from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.streaming_agent.gpio.led_controller import LedController


class DetectionLedControllerTests(unittest.TestCase):
    def test_defaults_to_gpio_14_and_15(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            controller = LedController()

        self.assertEqual(controller.pins, (14, 15))

    def test_empty_env_disables_detection_leds(self) -> None:
        with patch.dict(os.environ, {"DETECTION_LED_PINS": ""}, clear=True):
            controller = LedController()

        self.assertEqual(controller.pins, ())

    def test_env_overrides_detection_led_pins(self) -> None:
        with patch.dict(os.environ, {"DETECTION_LED_PINS": "20,21"}, clear=True):
            controller = LedController()

        self.assertEqual(controller.pins, (20, 21))


if __name__ == "__main__":
    unittest.main()

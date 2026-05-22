from __future__ import annotations

import unittest

from app.streaming_agent.gpio.led_controller import LedController


class DetectionLedControllerTests(unittest.TestCase):
    def test_legacy_led_controller_uses_relay_mapping(self) -> None:
        controller = LedController(active_low=True)

        self.assertEqual(controller.pins, (21, 20, 16, 12))
        self.assertEqual(controller.success_pin, 20)
        self.assertEqual(controller.failure_pin, 21)

    def test_set_active_tracks_red_and_buzzer_sources(self) -> None:
        controller = LedController(active_low=True)

        controller.set_active("person", True)

        self.assertIn("person", controller._red_sources)
        self.assertIn("person", controller._buzzer_sources)


if __name__ == "__main__":
    unittest.main()

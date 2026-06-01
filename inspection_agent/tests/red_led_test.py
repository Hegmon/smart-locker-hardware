from __future__ import annotations

"""Red LED relay inspection test."""

from inspection_agent.tests.base_test import BaseInspectionTest


class RedLedTest(BaseInspectionTest):
    module_name = "red_led"

    def execute(self) -> tuple[bool, str, dict | None]:
        self.context.relay_controller.pulse("red_led", duration_seconds=2.0)
        return True, "Red LED relay tested successfully", {"channel": "red_led", "gpio": 21, "duration_seconds": 2.0}

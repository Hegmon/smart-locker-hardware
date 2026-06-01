from __future__ import annotations

"""Green LED relay inspection test."""

from app.inspection_agent.tests.base_test import BaseInspectionTest


class GreenLedTest(BaseInspectionTest):
    module_name = "green_led"

    def execute(self) -> tuple[bool, str, dict | None]:
        self.context.relay_controller.pulse("green_led", duration_seconds=2.0)
        return True, "Green LED relay tested successfully", {"channel": "green_led", "gpio": 20, "duration_seconds": 2.0}

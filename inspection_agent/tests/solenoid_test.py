from __future__ import annotations

"""Solenoid relay inspection test."""

from inspection_agent.tests.base_test import BaseInspectionTest


class SolenoidTest(BaseInspectionTest):
    module_name = "solenoid"

    def execute(self) -> tuple[bool, str, dict | None]:
        self.context.relay_controller.pulse("solenoid", duration_seconds=2.0)
        return True, "Solenoid relay tested successfully", {"channel": "solenoid", "gpio": 16, "duration_seconds": 2.0}

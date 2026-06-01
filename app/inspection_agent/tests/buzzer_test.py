from __future__ import annotations

"""Buzzer relay inspection test."""

from app.inspection_agent.tests.base_test import BaseInspectionTest


class BuzzerTest(BaseInspectionTest):
    module_name = "buzzer"

    def execute(self) -> tuple[bool, str, dict | None]:
        self.context.relay_controller.pulse("buzzer", duration_seconds=1.0)
        return True, "Buzzer relay tested successfully", {"channel": "buzzer", "gpio": 12, "duration_seconds": 1.0}

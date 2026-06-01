from __future__ import annotations

"""External camera inspection test."""

from inspection_agent.tests.base_test import BaseInspectionTest


class ExternalCameraTest(BaseInspectionTest):
    module_name = "external_camera"

    def execute(self) -> tuple[bool, str, dict | None]:
        result = self.context.camera_controller.capture_frame("external")
        return result.captured, result.message, result.details

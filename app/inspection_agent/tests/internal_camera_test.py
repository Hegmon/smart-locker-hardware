from __future__ import annotations

"""Internal camera inspection test."""

from app.inspection_agent.tests.base_test import BaseInspectionTest


class InternalCameraTest(BaseInspectionTest):
    module_name = "internal_camera"

    def execute(self) -> tuple[bool, str, dict | None]:
        result = self.context.camera_controller.capture_frame("internal")
        return result.captured, result.message, result.details

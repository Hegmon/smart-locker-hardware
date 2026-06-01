from __future__ import annotations

"""Base primitives for inspection hardware tests."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from inspection_agent.hardware.camera_controller import CameraController
from inspection_agent.hardware.relay_controller import RelayController
from inspection_agent.schemas.inspection_response import InspectionResult


@dataclass
class InspectionTestContext:
    """Shared hardware dependencies for a single inspection test."""

    device_id: str
    camera_controller: CameraController
    relay_controller: RelayController


class BaseInspectionTest(ABC):
    """Common plumbing for a single inspection test."""

    module_name: str = "unknown"

    def __init__(
        self,
        *,
        device_id: str,
        camera_controller: CameraController,
        relay_controller: RelayController,
    ) -> None:
        self.context = InspectionTestContext(
            device_id=device_id,
            camera_controller=camera_controller,
            relay_controller=relay_controller,
        )

    @abstractmethod
    def execute(self) -> tuple[bool, str, dict[str, Any] | None]:
        """Run the concrete test and return pass/fail state."""

    def run(self, *, request_id: str = "") -> InspectionResult:
        try:
            passed, message, details = self.execute()
        except Exception as exc:
            return InspectionResult.failure(
                request_id=request_id,
                device_id=self.context.device_id,
                module=self.module_name,
                message=f"Test execution failed: {exc}",
            )
        if passed:
            return InspectionResult.success(
                request_id=request_id,
                device_id=self.context.device_id,
                module=self.module_name,
                message=message,
                details=details,
            )
        return InspectionResult.failure(
            request_id=request_id,
            device_id=self.context.device_id,
            module=self.module_name,
            message=message,
            details=details,
        )

from __future__ import annotations

"""Registry-based inspection test manager."""

from dataclasses import dataclass
from contextlib import contextmanager
from typing import Type

from app.deployment.runtime_config import get_float_setting, get_str_setting
from app.inspection_agent.hardware.camera_controller import CameraController
from app.inspection_agent.hardware.relay_controller import RelayController
from app.inspection_agent.hardware.streaming_service_controller import StreamingServiceController
from app.inspection_agent.schemas.inspection_response import InspectionResult, InspectionSummary
from app.inspection_agent.tests.base_test import BaseInspectionTest
from app.inspection_agent.tests.buzzer_test import BuzzerTest
from app.inspection_agent.tests.external_camera_test import ExternalCameraTest
from app.inspection_agent.tests.green_led_test import GreenLedTest
from app.inspection_agent.tests.internal_camera_test import InternalCameraTest
from app.inspection_agent.tests.red_led_test import RedLedTest
from app.inspection_agent.tests.solenoid_test import SolenoidTest
from app.utils.logger import get_logger


logger = get_logger(__name__)


TESTS: dict[str, Type[BaseInspectionTest]] = {
    "internal_camera": InternalCameraTest,
    "external_camera": ExternalCameraTest,
    "red_led": RedLedTest,
    "green_led": GreenLedTest,
    "buzzer": BuzzerTest,
    "solenoid": SolenoidTest,
}


@dataclass
class InspectionHardwareBundle:
    """Shared hardware dependencies used by inspection tests."""

    camera_controller: CameraController
    relay_controller: RelayController


class InspectionAgentManager:
    """Coordinates inspection tests without coupling to locker workflows."""

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.camera_runtime_service = get_str_setting("INSPECTION_CAMERA_RUNTIME_SERVICE", "qbox-device.service")
        self.camera_settle_seconds = get_float_setting("INSPECTION_CAMERA_SETTLE_SECONDS", 2.0)
        self.hardware = InspectionHardwareBundle(
            camera_controller=CameraController(),
            relay_controller=RelayController(),
        )
        self.streaming_services = StreamingServiceController()

    def run_test(self, module_name: str, *, request_id: str = "") -> InspectionResult:
        module_key = self._normalize_module_name(module_name)
        test_class = TESTS.get(module_key)
        if test_class is None:
            message = f"Unknown inspection module: {module_name}"
            logger.warning(message)
            return InspectionResult.failure(
                request_id=request_id,
                device_id=self.device_id,
                module=module_key or module_name,
                message=message,
            )

        test = test_class(
            device_id=self.device_id,
            camera_controller=self.hardware.camera_controller,
            relay_controller=self.hardware.relay_controller,
        )
        try:
            with self._camera_runtime_guard(module_key):
                result = test.run(request_id=request_id)
        except Exception as exc:
            logger.exception("Inspection test crashed: module=%s", module_key)
            result = InspectionResult.failure(
                request_id=request_id,
                device_id=self.device_id,
                module=module_key,
                message=f"Inspection test raised an exception: {exc}",
            )
        return result

    def run_all_tests(self, *, request_id: str = "") -> tuple[list[InspectionResult], InspectionSummary]:
        results: list[InspectionResult] = []
        passed = 0
        failed = 0
        camera_runtime_active = self.streaming_services.is_active(self.camera_runtime_service)
        camera_runtime_stopped = False

        if camera_runtime_active:
            camera_runtime_stopped = self.streaming_services.stop_service(self.camera_runtime_service)
            if camera_runtime_stopped and self.camera_settle_seconds > 0:
                import time

                time.sleep(self.camera_settle_seconds)

        try:
            for module_name in TESTS:
                test_class = TESTS.get(module_name)
                if test_class is None:
                    continue

                test = test_class(
                    device_id=self.device_id,
                    camera_controller=self.hardware.camera_controller,
                    relay_controller=self.hardware.relay_controller,
                )
                try:
                    result = test.run(request_id=request_id)
                except Exception as exc:
                    logger.exception("Inspection test crashed: module=%s", module_name)
                    result = InspectionResult.failure(
                        request_id=request_id,
                        device_id=self.device_id,
                        module=module_name,
                        message=f"Inspection test raised an exception: {exc}",
                    )

                results.append(result)
                if result.status == "PASS":
                    passed += 1
                else:
                    failed += 1

                if module_name == "external_camera" and camera_runtime_stopped:
                    self.streaming_services.start_service(self.camera_runtime_service)
                    camera_runtime_stopped = False
        finally:
            if camera_runtime_stopped:
                self.streaming_services.start_service(self.camera_runtime_service)

        summary = InspectionSummary.from_counts(
            request_id=request_id,
            passed=passed,
            failed=failed,
        )
        logger.info(
            "Inspection run_all completed device_id=%s passed=%s failed=%s",
            self.device_id,
            passed,
            failed,
        )
        return results, summary

    @staticmethod
    def _normalize_module_name(module_name: str) -> str:
        return str(module_name or "").strip().lower()

    @contextmanager
    def _camera_runtime_guard(self, module_name: str):
        if module_name not in {"internal_camera", "external_camera"}:
            yield
            return

        was_active = self.streaming_services.is_active(self.camera_runtime_service)
        if was_active:
            self.streaming_services.stop_service(self.camera_runtime_service)
            if self.camera_settle_seconds > 0:
                import time

                time.sleep(self.camera_settle_seconds)
        try:
            yield
        finally:
            if was_active:
                self.streaming_services.start_service(self.camera_runtime_service)

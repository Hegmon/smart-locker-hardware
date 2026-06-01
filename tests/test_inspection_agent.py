from __future__ import annotations

import unittest
from unittest.mock import patch

from app.inspection_agent.manager import InspectionAgentManager
from app.inspection_agent.schemas.inspection_request import InspectionRequest
from app.inspection_agent.schemas.inspection_response import InspectionResult, InspectionSummary


class InspectionRequestTests(unittest.TestCase):
    def test_request_payload_is_normalized(self) -> None:
        request = InspectionRequest.from_payload(
            {
                "request_id": "abc-123",
                "action": "RUN_TEST",
                "module": "Internal_Camera",
            }
        )
        self.assertEqual(request.request_id, "abc-123")
        self.assertEqual(request.action, "run_test")
        self.assertEqual(request.module, "internal_camera")

    def test_request_payload_requires_request_id(self) -> None:
        with self.assertRaises(ValueError):
            InspectionRequest.from_payload({"action": "run_all"})


class InspectionManagerTests(unittest.TestCase):
    def test_unknown_module_returns_fail_result(self) -> None:
        manager = InspectionAgentManager(device_id="SL001")
        result = manager.run_test("does_not_exist", request_id="req-1")
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.module, "does_not_exist")
        self.assertEqual(result.request_id, "req-1")

    def test_run_all_uses_registry_entries(self) -> None:
        manager = InspectionAgentManager(device_id="SL001")

        class _FakeTest:
            def __init__(self, *, device_id: str, camera_controller, relay_controller) -> None:
                self.device_id = device_id

            def run(self, *, request_id: str = "") -> InspectionResult:
                return InspectionResult.success(
                    request_id=request_id,
                    device_id="SL001",
                    module="fake",
                    message="ok",
                )

        with patch("app.inspection_agent.manager.TESTS", {"fake": _FakeTest}):
            results, summary = manager.run_all_tests(request_id="req-2")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "PASS")
        self.assertIsInstance(summary, InspectionSummary)
        self.assertEqual(summary.passed, 1)
        self.assertEqual(summary.failed, 0)

    def test_internal_camera_resolution_prefers_detected_usb_camera(self) -> None:
        from app.inspection_agent.hardware.camera_controller import CameraController

        controller = CameraController()
        with (
            patch(
                "app.streaming_agent.camera_detector.subprocess.run",
                return_value=type(
                    "_Result",
                    (),
                    {
                        "stdout": "USB Camera (usb-0000:00:14.0-1.2):\n    /dev/video2\n\nUSB Camera (usb-0000:00:14.0-1.4):\n    /dev/video7\n",
                        "returncode": 0,
                    }
                )(),
            ),
            patch("app.streaming_agent.camera_detector.Path.exists", return_value=True),
        ):
            candidates = controller.resolve_device_candidates("internal")

        self.assertGreaterEqual(len(candidates), 2)
        self.assertEqual(candidates[0], "/dev/video2")

    def test_camera_tests_pause_and_restore_streaming_services(self) -> None:
        manager = InspectionAgentManager(device_id="SL001")

        class _FakeStreamingServices:
            def __init__(self) -> None:
                self.stopped = []
                self.started = []

            def stop_streaming_services(self, *, timeout_seconds: float = 12.0):
                self.stopped.append(timeout_seconds)
                return type("_Control", (), {"stopped_services": ("qbox-device.service",)})()

            def start_streaming_services(self, services, *, timeout_seconds: float = 12.0):
                self.started.append((services, timeout_seconds))

        class _FakeCameraTest:
            def __init__(self, *, device_id: str, camera_controller, relay_controller) -> None:
                pass

            def run(self, *, request_id: str = "") -> InspectionResult:
                return InspectionResult.success(
                    request_id=request_id,
                    device_id="SL001",
                    module="internal_camera",
                    message="ok",
                )

        manager.streaming_services = _FakeStreamingServices()
        with patch("app.inspection_agent.manager.TESTS", {"internal_camera": _FakeCameraTest}):
            result = manager.run_test("internal_camera", request_id="req-3")

        self.assertEqual(result.status, "PASS")
        self.assertEqual(manager.streaming_services.stopped, [12.0])
        self.assertEqual(manager.streaming_services.started, [(("qbox-device.service",), 12.0)])


if __name__ == "__main__":
    unittest.main()

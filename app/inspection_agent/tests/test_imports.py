from __future__ import annotations

"""Smoke tests for the app.inspection_agent package."""

import unittest

from app.inspection_agent.manager import InspectionAgentManager
from app.inspection_agent.schemas.inspection_request import InspectionRequest


class AppInspectionAgentSmokeTests(unittest.TestCase):
    def test_request_normalizes_action(self) -> None:
        request = InspectionRequest.from_payload({"request_id": "x", "action": "RUN_ALL"})
        self.assertEqual(request.action, "run_all")

    def test_manager_constructs(self) -> None:
        manager = InspectionAgentManager(device_id="SL001")
        self.assertEqual(manager.device_id, "SL001")


if __name__ == "__main__":
    unittest.main()

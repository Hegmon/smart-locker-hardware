from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.hardware_agent.saved_networks import SavedNetworkManager


class SavedNetworkManagerTests(unittest.TestCase):
    def test_marks_failure_with_exponential_backoff_without_passwords(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "wifi_state.json"
            manager = SavedNetworkManager(
                state_file,
                retry_base_delay_seconds=2,
                max_retry_delay_seconds=30,
            )

            with patch("app.hardware_agent.saved_networks.list_saved_wifi_networks", return_value=["home"]):
                manager.list()
                manager.mark_failure("home", "auth failed")
                manager.mark_failure("home", "auth failed")
                records = manager.list()

            self.assertEqual(records[0].ssid, "home")
            self.assertEqual(records[0].failure_count, 2)
            self.assertIn("auth failed", state_file.read_text(encoding="utf-8"))
            self.assertNotIn("password", state_file.read_text(encoding="utf-8").lower())

    def test_success_resets_failure_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = SavedNetworkManager(
                Path(temp_dir) / "wifi_state.json",
                retry_base_delay_seconds=2,
                max_retry_delay_seconds=30,
            )

            manager.mark_failure("office", "dhcp failed")
            manager.mark_success("office")

            with patch("app.hardware_agent.saved_networks.list_saved_wifi_networks", return_value=["office"]):
                records = manager.list()

            self.assertEqual(records[0].failure_count, 0)
            self.assertEqual(records[0].last_failure_reason, "")
            self.assertEqual(records[0].backoff_until, 0.0)


if __name__ == "__main__":
    unittest.main()

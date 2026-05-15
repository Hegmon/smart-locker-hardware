from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services import wifi_manager


class WifiManagerTests(unittest.TestCase):
    def test_connected_details_uses_device_status_when_device_show_fails(self) -> None:
        with (
            patch("app.services.wifi_manager.ensure_wifi_radio"),
            patch("app.services.wifi_manager._connection_summary", side_effect=wifi_manager.WifiCommandError("bad field")),
            patch(
                "app.services.wifi_manager._device_status_summary",
                return_value={"profile": "ReplaITSolution", "state": "connected"},
            ),
            patch(
                "app.services.wifi_manager._active_connection_for_interface",
                return_value={"profile": "ReplaITSolution", "device": "wlan0"},
            ),
            patch("app.services.wifi_manager._active_wifi_details", side_effect=wifi_manager.WifiCommandError("scan unavailable")),
        ):
            details = wifi_manager.get_connected_wifi_details()

        self.assertTrue(details["connected"])
        self.assertEqual(details["connected_ssid"], "ReplaITSolution")
        self.assertEqual(details["connection_profile"], "ReplaITSolution")

    def test_reconnect_success_reenables_autoconnect(self) -> None:
        nmcli_calls: list[list[str]] = []

        def fake_nmcli(args, **kwargs):
            nmcli_calls.append(args)
            return _Result(stdout="ok")

        with (
            patch("app.services.wifi_manager.ensure_wifi_radio"),
            patch("app.services.wifi_manager.stop_hotspot"),
            patch("app.services.wifi_manager._saved_profile_exists", return_value=True),
            patch("app.services.wifi_manager._nmcli", side_effect=fake_nmcli),
            patch("app.services.wifi_manager._wait_for_connection", return_value=True),
            patch(
                "app.services.wifi_manager.get_connected_wifi_details",
                return_value={"connected": True, "connected_ssid": "Amk"},
            ),
        ):
            result = wifi_manager.reconnect_saved_wifi("Amk")

        self.assertEqual(result["status"], "reconnected")
        self.assertIn(
            ["connection", "modify", "id", "Amk", "connection.autoconnect", "yes"],
            nmcli_calls,
        )

    def test_failed_saved_reconnect_only_cancels_target_profile(self) -> None:
        with (
            patch("app.services.wifi_manager.ensure_wifi_radio"),
            patch("app.services.wifi_manager.stop_hotspot"),
            patch("app.services.wifi_manager._saved_profile_exists", return_value=True),
            patch("app.services.wifi_manager._nmcli", side_effect=wifi_manager.WifiCommandError("activation failed")),
            patch("app.services.wifi_manager._cancel_profile_activation") as cancel_profile,
            patch("app.services.wifi_manager._cancel_wifi_activation") as cancel_device,
        ):
            with self.assertRaises(wifi_manager.WifiCommandError):
                wifi_manager.reconnect_saved_wifi("Amk")

        cancel_profile.assert_called_once_with("Amk")
        cancel_device.assert_not_called()

    def test_wait_for_connection_does_not_accept_activating_profile_as_connected(self) -> None:
        with patch(
            "app.services.wifi_manager.get_connected_wifi_details",
            return_value={
                "connected": False,
                "connected_ssid": "",
                "connection_profile": "Amk",
                "device_state": "70 (getting IP configuration)",
            },
        ):
            self.assertFalse(wifi_manager._wait_for_connection("Amk", timeout=0.01))

    def test_wait_for_connection_accepts_active_connected_ssid_before_state_100(self) -> None:
        with patch(
            "app.services.wifi_manager.get_connected_wifi_details",
            return_value={
                "connected": True,
                "connected_ssid": "Amk",
                "connection_profile": "",
                "device_state": "70 (getting IP configuration)",
            },
        ):
            self.assertTrue(wifi_manager._wait_for_connection("Amk", timeout=0.01))

    def test_connect_wifi_accepts_success_after_nmcli_timeout_grace(self) -> None:
        nmcli_calls: list[list[str]] = []

        def fake_nmcli(args, **kwargs):
            nmcli_calls.append(args)
            if args[:2] == ["-g", "802-11-wireless-security.psk-flags"]:
                return _Result(stdout="0")
            if args[:4] == ["connection", "up", "id", "Amk"]:
                raise wifi_manager.WifiCommandError("Timeout: nmcli --wait 45 connection up id Amk ifname wlan0")
            return _Result(stdout="ok")

        with (
            patch("app.services.wifi_manager.ensure_wifi_radio"),
            patch("app.services.wifi_manager.stop_hotspot"),
            patch("app.services.wifi_manager._create_wifi_profile"),
            patch("app.services.wifi_manager._nmcli", side_effect=fake_nmcli),
            patch("app.services.wifi_manager._wait_for_connection", return_value=True),
            patch(
                "app.services.wifi_manager.get_connected_wifi_details",
                return_value={"connected": True, "connected_ssid": "Amk", "device_state": "100 (connected)"},
            ),
            patch("app.services.wifi_manager._cancel_wifi_activation") as cancel_wifi,
            patch("app.services.wifi_manager._delete_saved_profile") as delete_profile,
        ):
            result = wifi_manager.connect_wifi("Amk", "secret")

        self.assertEqual(result["status"], "connected")
        self.assertEqual(result["details"], "connected after nmcli timeout")
        cancel_wifi.assert_not_called()
        delete_profile.assert_not_called()
        self.assertIn(
            ["connection", "modify", "id", "Amk", "connection.autoconnect", "yes"],
            nmcli_calls,
        )

    def test_connect_wifi_accepts_success_after_nmcli_timeout_expired_message(self) -> None:
        def fake_nmcli(args, **kwargs):
            if args[:2] == ["-g", "802-11-wireless-security.psk-flags"]:
                return _Result(stdout="0")
            if args[:4] == ["connection", "up", "id", "Amk"]:
                raise wifi_manager.WifiCommandError(
                    "nmcli --wait 5 connection up id Amk ifname wlan0: Error: Timeout expired (5 seconds)"
                )
            return _Result(stdout="ok")

        with (
            patch("app.services.wifi_manager.ensure_wifi_radio"),
            patch("app.services.wifi_manager.stop_hotspot"),
            patch("app.services.wifi_manager._create_wifi_profile"),
            patch("app.services.wifi_manager._nmcli", side_effect=fake_nmcli),
            patch("app.services.wifi_manager._wait_for_connection", return_value=True),
            patch(
                "app.services.wifi_manager.get_connected_wifi_details",
                return_value={"connected": True, "connected_ssid": "Amk", "device_state": "100 (connected)"},
            ),
            patch("app.services.wifi_manager._cancel_wifi_activation") as cancel_wifi,
            patch("app.services.wifi_manager._delete_saved_profile") as delete_profile,
        ):
            result = wifi_manager.connect_wifi(
                "Amk",
                "secret",
                activation_timeout=5,
                connection_wait_timeout=8,
            )

        self.assertEqual(result["status"], "connected")
        cancel_wifi.assert_not_called()
        delete_profile.assert_not_called()

    def test_connect_wifi_keeps_active_connection_after_timeout_wait_failure(self) -> None:
        def fake_nmcli(args, **kwargs):
            if args[:2] == ["-g", "802-11-wireless-security.psk-flags"]:
                return _Result(stdout="0")
            if args[:4] == ["connection", "up", "id", "Amk"]:
                raise wifi_manager.WifiCommandError(
                    "nmcli --wait 5 connection up id Amk ifname wlan0: Error: Timeout expired (5 seconds)"
                )
            return _Result(stdout="ok")

        with (
            patch("app.services.wifi_manager.ensure_wifi_radio"),
            patch("app.services.wifi_manager.stop_hotspot"),
            patch("app.services.wifi_manager._create_wifi_profile"),
            patch("app.services.wifi_manager._nmcli", side_effect=fake_nmcli),
            patch("app.services.wifi_manager._wait_for_connection", return_value=False),
            patch(
                "app.services.wifi_manager.get_connected_wifi_details",
                return_value={
                    "connected": True,
                    "connected_ssid": "Amk",
                    "connection_profile": "",
                    "device_state": "70 (getting IP configuration)",
                },
            ),
            patch("app.services.wifi_manager._cancel_wifi_activation") as cancel_wifi,
            patch("app.services.wifi_manager._delete_saved_profile") as delete_profile,
        ):
            result = wifi_manager.connect_wifi(
                "Amk",
                "secret",
                activation_timeout=5,
                connection_wait_timeout=8,
            )

        self.assertEqual(result["status"], "connected")
        self.assertEqual(result["connection"]["connected_ssid"], "Amk")
        cancel_wifi.assert_not_called()
        delete_profile.assert_not_called()

    def test_connect_wifi_uses_remote_activation_timeout(self) -> None:
        nmcli_timeouts: list[int] = []

        def fake_nmcli(args, **kwargs):
            nmcli_timeouts.append(kwargs.get("timeout"))
            if args[:2] == ["-g", "802-11-wireless-security.psk-flags"]:
                return _Result(stdout="0")
            return _Result(stdout="ok")

        with (
            patch("app.services.wifi_manager.ensure_wifi_radio"),
            patch("app.services.wifi_manager.stop_hotspot"),
            patch("app.services.wifi_manager._create_wifi_profile"),
            patch("app.services.wifi_manager._nmcli", side_effect=fake_nmcli),
            patch("app.services.wifi_manager._wait_for_connection", return_value=True) as wait_for_connection,
            patch(
                "app.services.wifi_manager.get_connected_wifi_details",
                return_value={"connected": True, "connected_ssid": "Amk", "device_state": "100 (connected)"},
            ),
        ):
            result = wifi_manager.connect_wifi(
                "Amk",
                "secret",
                activation_timeout=5,
                connection_wait_timeout=8,
            )

        self.assertEqual(result["status"], "connected")
        self.assertIn(5, nmcli_timeouts)
        wait_for_connection.assert_called_once_with("Amk", timeout=8)


class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


if __name__ == "__main__":
    unittest.main()

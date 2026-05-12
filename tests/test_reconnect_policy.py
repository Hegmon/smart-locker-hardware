from __future__ import annotations

import unittest

from app.hardware_agent.reconnect_policy import (
    ReconnectPolicy,
    ReconnectPolicyConfig,
    SavedNetwork,
    ScannedNetwork,
)


class ReconnectPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ReconnectPolicy(
            ReconnectPolicyConfig(
                minimum_signal_dbm=-70,
                switch_hysteresis_dbm=10,
                switch_cooldown_seconds=180,
            )
        )

    def test_threshold_ignores_weak_networks(self) -> None:
        candidates = self.policy.build_candidates(
            [
                ScannedNetwork("weak", -80),
                ScannedNetwork("strong", -45),
            ],
            [
                SavedNetwork("weak"),
                SavedNetwork("strong"),
            ],
            now=1000,
        )

        self.assertEqual([candidate.ssid for candidate in candidates], ["strong"])

    def test_rssi_comparison_prefers_closer_to_zero(self) -> None:
        candidates = self.policy.build_candidates(
            [
                ScannedNetwork("office", -65),
                ScannedNetwork("home", -45),
            ],
            [
                SavedNetwork("office"),
                SavedNetwork("home"),
            ],
            now=1000,
        )

        self.assertEqual(candidates[0].ssid, "home")

    def test_percentage_signal_is_normalized(self) -> None:
        self.assertEqual(self.policy.normalize_rssi(100), -50)
        self.assertEqual(self.policy.normalize_rssi(60), -70)
        self.assertEqual(self.policy.normalize_rssi(-45), -45)

    def test_hysteresis_prevents_flapping(self) -> None:
        candidate = self.policy.build_candidates(
            [ScannedNetwork("backup", -58)],
            [SavedNetwork("backup")],
            now=1000,
        )[0]

        should_switch, reason = self.policy.should_switch(
            current_ssid="primary",
            current_rssi=-65,
            candidate=candidate,
            last_switch_at=0,
            now=1000,
        )

        self.assertFalse(should_switch)
        self.assertEqual(reason, "candidate not significantly stronger")

    def test_hysteresis_allows_significantly_better_network(self) -> None:
        candidate = self.policy.build_candidates(
            [ScannedNetwork("backup", -50)],
            [SavedNetwork("backup")],
            now=1000,
        )[0]

        should_switch, reason = self.policy.should_switch(
            current_ssid="primary",
            current_rssi=-65,
            candidate=candidate,
            last_switch_at=0,
            now=1000,
        )

        self.assertTrue(should_switch)
        self.assertEqual(reason, "candidate exceeds hysteresis")

    def test_switch_cooldown_prevents_recent_switch(self) -> None:
        candidate = self.policy.build_candidates(
            [ScannedNetwork("backup", -45)],
            [SavedNetwork("backup")],
            now=1000,
        )[0]

        should_switch, reason = self.policy.should_switch(
            current_ssid="primary",
            current_rssi=-65,
            candidate=candidate,
            last_switch_at=950,
            now=1000,
        )

        self.assertFalse(should_switch)
        self.assertEqual(reason, "switch cooldown active")

    def test_no_saved_network_returns_no_candidates(self) -> None:
        candidates = self.policy.build_candidates(
            [ScannedNetwork("visible", -40)],
            [],
            now=1000,
        )

        self.assertEqual(candidates, [])

    def test_backoff_excludes_failed_network(self) -> None:
        candidates = self.policy.build_candidates(
            [ScannedNetwork("home", -40)],
            [SavedNetwork("home", failure_count=3, backoff_until=2000)],
            now=1000,
        )

        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()

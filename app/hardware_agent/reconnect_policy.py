from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ScannedNetwork:
    ssid: str
    rssi: int
    security: str = "UNKNOWN"


@dataclass(frozen=True)
class SavedNetwork:
    ssid: str
    priority: int = 0
    last_success_at: float = 0.0
    failure_count: int = 0
    backoff_until: float = 0.0


@dataclass(frozen=True)
class NetworkCandidate:
    ssid: str
    rssi: int
    priority: int
    last_success_at: float
    failure_count: int
    score: tuple[int, int, float, int]


@dataclass(frozen=True)
class ReconnectPolicyConfig:
    minimum_signal_dbm: int = -70
    switch_hysteresis_dbm: int = 10
    switch_cooldown_seconds: int = 180


class ReconnectPolicy:
    def __init__(self, config: ReconnectPolicyConfig) -> None:
        self.config = config

    @staticmethod
    def normalize_rssi(value: int | float | None) -> int:
        if value is None:
            return -999
        numeric = int(value)
        if 0 <= numeric <= 100:
            return int((numeric / 2) - 100)
        return numeric

    def build_candidates(
        self,
        scanned_networks: list[object],
        saved_networks: list[SavedNetwork],
        *,
        now: float | None = None,
    ) -> list[NetworkCandidate]:
        now = time.time() if now is None else now
        saved_by_ssid = {network.ssid: network for network in saved_networks}
        best_scan_by_ssid: dict[str, object] = {}

        for network in scanned_networks:
            ssid = str(getattr(network, "ssid", "") or "").strip()
            if not ssid or ssid not in saved_by_ssid:
                continue
            rssi = self.normalize_rssi(getattr(network, "rssi", None))
            if rssi < self.config.minimum_signal_dbm:
                continue
            existing = best_scan_by_ssid.get(ssid)
            if existing is None or rssi > self.normalize_rssi(getattr(existing, "rssi", None)):
                best_scan_by_ssid[ssid] = network

        candidates: list[NetworkCandidate] = []
        for ssid, network in best_scan_by_ssid.items():
            saved = saved_by_ssid[ssid]
            if saved.backoff_until and saved.backoff_until > now:
                continue
            rssi = self.normalize_rssi(getattr(network, "rssi", None))
            score = (rssi, saved.priority, saved.last_success_at, -saved.failure_count)
            candidates.append(
                NetworkCandidate(
                    ssid=ssid,
                    rssi=rssi,
                    priority=saved.priority,
                    last_success_at=saved.last_success_at,
                    failure_count=saved.failure_count,
                    score=score,
                )
            )

        return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)

    def should_switch(
        self,
        *,
        current_ssid: str | None,
        current_rssi: int | None,
        candidate: NetworkCandidate | None,
        last_switch_at: float,
        now: float | None = None,
    ) -> tuple[bool, str]:
        if candidate is None:
            return False, "no candidate"

        now = time.monotonic() if now is None else now
        if not current_ssid:
            return True, "not connected"

        if candidate.ssid == current_ssid:
            return False, "already on best candidate"

        if now - last_switch_at < self.config.switch_cooldown_seconds:
            return False, "switch cooldown active"

        normalized_current_rssi = self.normalize_rssi(current_rssi)
        if normalized_current_rssi < self.config.minimum_signal_dbm:
            return True, "current signal below threshold"

        if candidate.rssi >= normalized_current_rssi + self.config.switch_hysteresis_dbm:
            return True, "candidate exceeds hysteresis"

        return False, "candidate not significantly stronger"

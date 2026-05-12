from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.hardware_agent.reconnect_policy import SavedNetwork
from app.services.wifi_manager import list_saved_wifi_networks
from app.utils.logger import get_logger


logger = get_logger(__name__)


@dataclass
class SavedNetworkRecord:
    ssid: str
    priority: int = 0
    last_success_at: float = 0.0
    failure_count: int = 0
    last_failure_reason: str = ""
    backoff_until: float = 0.0

    def to_policy_network(self) -> SavedNetwork:
        return SavedNetwork(
            ssid=self.ssid,
            priority=self.priority,
            last_success_at=self.last_success_at,
            failure_count=self.failure_count,
            backoff_until=self.backoff_until,
        )


class SavedNetworkManager:
    def __init__(
        self,
        state_file: Path,
        *,
        retry_base_delay_seconds: float,
        max_retry_delay_seconds: int,
    ) -> None:
        self.state_file = state_file
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.max_retry_delay_seconds = max_retry_delay_seconds
        self._records: dict[str, SavedNetworkRecord] = {}
        self._raw_state: dict[str, Any] = {}
        self._load()

    def list(self) -> list[SavedNetworkRecord]:
        nm_ssids = list_saved_wifi_networks()
        for ssid in nm_ssids:
            self._records.setdefault(ssid, SavedNetworkRecord(ssid=ssid))

        stale_ssids = [ssid for ssid in self._records if ssid not in nm_ssids]
        for ssid in stale_ssids:
            self._records.pop(ssid, None)

        self._save()
        return sorted(
            self._records.values(),
            key=lambda record: (record.priority, record.last_success_at, -record.failure_count, record.ssid),
            reverse=True,
        )

    def policy_networks(self) -> list[SavedNetwork]:
        return [record.to_policy_network() for record in self.list()]

    def mark_success(self, ssid: str) -> None:
        record = self._records.setdefault(ssid, SavedNetworkRecord(ssid=ssid))
        record.last_success_at = time.time()
        record.failure_count = 0
        record.last_failure_reason = ""
        record.backoff_until = 0.0
        self._save()

    def mark_failure(self, ssid: str, reason: str) -> None:
        record = self._records.setdefault(ssid, SavedNetworkRecord(ssid=ssid))
        record.failure_count += 1
        record.last_failure_reason = self._sanitize_reason(reason)
        delay = self._failure_delay(record.failure_count, record.last_failure_reason)
        record.backoff_until = time.time() + delay
        self._save()

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._records = {}
            return
        except Exception:
            logger.exception("Saved WiFi metadata load failed; starting with empty metadata")
            self._records = {}
            return

        self._raw_state = raw if isinstance(raw, dict) else {}
        records = self._raw_state.get("saved_networks") if isinstance(self._raw_state, dict) else {}
        if not isinstance(records, dict):
            self._records = {}
            return

        self._records = {}
        for ssid, payload in records.items():
            if not isinstance(payload, dict):
                continue
            self._records[str(ssid)] = SavedNetworkRecord(
                ssid=str(ssid),
                priority=int(payload.get("priority") or 0),
                last_success_at=float(payload.get("last_success_at") or 0.0),
                failure_count=int(payload.get("failure_count") or 0),
                last_failure_reason=str(payload.get("last_failure_reason") or ""),
                backoff_until=float(payload.get("backoff_until") or 0.0),
            )

    def _save(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, Any] = dict(self._raw_state)
            payload["saved_networks"] = {
                ssid: asdict(record)
                for ssid, record in sorted(self._records.items())
            }
            self._raw_state = payload
            self.state_file.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            logger.exception("Saved WiFi metadata save failed")

    @staticmethod
    def _sanitize_reason(reason: str) -> str:
        text = str(reason or "").replace("\n", " ").strip()
        return text[:240]

    def _failure_delay(self, failure_count: int, reason: str) -> float:
        lowered = reason.lower()
        if "authentication failed" in lowered or "wrong or missing wifi password" in lowered:
            return max(self.max_retry_delay_seconds, 900)
        return min(
            self.max_retry_delay_seconds,
            self.retry_base_delay_seconds * (2 ** max(0, failure_count - 1)),
        )

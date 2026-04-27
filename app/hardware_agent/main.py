from __future__ import annotations

import hashlib
import json
import random
import time
from datetime import datetime, timezone
from typing import Any

from app.hardware_agent.client import WifiApiClient, WifiApiError
from app.hardware_agent.config import AgentConfig, load_agent_config
from app.hardware_agent.scanner import WifiNetwork, WifiScanner, WifiScannerError
from app.hardware_agent.storage import JsonFileStorage, QueueItem
from app.services.wifi_manager import (
    WifiCommandError,
    connect_wifi,
    get_connected_wifi_details,
)
from app.utils.logger import get_logger


logger = get_logger(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class WifiUploadAgent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.scanner = WifiScanner(config.interface)
        self.storage = JsonFileStorage(config.state_file, config.queue_file)
        self.client = WifiApiClient(timeout_seconds=config.request_timeout_seconds)

    def run_forever(self) -> None:
        logger.info("Starting WiFi upload agent (30s interval)")
        while True:
            start = time.monotonic()
            try:
                self.run_once()
            except Exception:
                logger.exception("Agent loop failed (handled safely)")

            elapsed = time.monotonic() - start
            sleep_time = max(1, 30 - int(elapsed))  # 👈 fixed 30 sec interval
            print(f"[agent] loop done in {elapsed:.1f}s, sleeping {sleep_time}s", flush=True)
            time.sleep(sleep_time)

    def run_once(self) -> None:
        # ✅ Load state ONCE
        state = self.storage.load_state() or {}

        self._flush_queue()

        connected_state = self._get_connected_state()

        print("[agent] Starting WiFi scan", flush=True)

        try:
            networks = self.scanner.scan()
        except WifiScannerError as exc:
            logger.exception("WiFi scan failed")
            return

        print(f"[agent] Found {len(networks)} networks", flush=True)

        # ✅ Process everything with SAME state object
        self._maybe_report_scan_diff(networks, connected_state, state)

    # ✅ PASS STATE (no re-load)
    def _maybe_report_scan_diff(
        self,
        networks: list[WifiNetwork],
        connected_state: dict[str, Any],
        state: dict[str, Any],
    ) -> None:

        previous_networks = self._extract_network_map(state.get("last_scan_networks"))

        current_networks = {n.ssid: n for n in networks if n.ssid}

        diff_payload = self._build_scan_diff_payload(
            previous_networks, current_networks, connected_state
        )

        has_diff = bool(
            diff_payload["new_networks"]
            or diff_payload["removed_networks"]
            or diff_payload["updated_networks"]
        )

        last_sent_at = self._parse_epoch(state.get("last_scan_reported_at_epoch"))

        heartbeat_due = last_sent_at is None or (time.time() - last_sent_at) >= 300

        # update state BEFORE sending
        state["last_scan_networks"] = {
            ssid: net.to_payload() for ssid, net in current_networks.items()
        }
        state["last_scan_at"] = diff_payload["timestamp"]

        self.storage.save_state(state)

        if not has_diff and not heartbeat_due:
            print("[agent] No changes, skipping", flush=True)
            return

        print("[agent] Sending scan update...", flush=True)

        if self._send_with_retry("scan_event", self.config.scan_event_url, diff_payload):
            state["last_scan_reported_at"] = diff_payload["timestamp"]
            state["last_scan_reported_at_epoch"] = time.time()
            self.storage.save_state(state)
        else:
            self.storage.append_queue_item(
                QueueItem(kind="scan_event", payload=diff_payload, retry_count=0)
            )

    def _flush_queue(self) -> None:
        try:
            queue = self.storage.load_queue()
        except Exception:
            return

        if not queue:
            return

        for item in queue[:5]:
            if not self._deliver_queue_item(item):
                return

        self.storage.save_queue(queue[5:])

    def _deliver_queue_item(self, item: QueueItem) -> bool:
        try:
            return self._send_with_retry(item.kind, self.config.scan_event_url, item.payload)
        except Exception:
            return False

    def _send_with_retry(self, kind: str, url: str, payload: dict[str, Any]) -> bool:
        try:
            self.client.post_json(url, payload)
            logger.info("%s success", kind)
            return True
        except WifiApiError as exc:
            logger.warning("%s failed: %s", kind, exc)
            return False

    def _build_scan_diff_payload(
        self,
        previous: dict[str, WifiNetwork],
        current: dict[str, WifiNetwork],
        connected_state: dict[str, Any],
    ) -> dict[str, Any]:

        new = [n.to_payload() for s, n in current.items() if s not in previous]
        removed = [previous[s].to_payload() for s in previous if s not in current]

        return {
            "device_uuid": self.config.device_uuid,
            "device_id": self.config.device_id,
            "connected_ssid": connected_state["connected_ssid"],
            "signal_strength": connected_state["signal_strength"],
            "timestamp": utc_now_iso(),
            "new_networks": new,
            "removed_networks": removed,
            "updated_networks": [],
        }

    @staticmethod
    def _extract_network_map(raw: Any) -> dict[str, WifiNetwork]:
        if not isinstance(raw, dict):
            return {}

        return {
            ssid: WifiNetwork(
                ssid=ssid,
                rssi=int(v.get("rssi", -100)),
                is_secured=bool(v.get("is_secured", False)),
            )
            for ssid, v in raw.items()
            if isinstance(v, dict)
        }

    @staticmethod
    def _get_connected_state() -> dict[str, Any]:
        d = get_connected_wifi_details()
        return {
            "connected_ssid": str(d.get("connected_ssid") or ""),
            "signal_strength": int(d.get("signal_strength") or 0),
            "rssi": int(d.get("rssi") or -100),
            "status": "CONNECTED" if d.get("connected") else "DISCONNECTED",
        }

    @staticmethod
    def _parse_epoch(value: Any) -> float | None:
        try:
            return float(value)
        except Exception:
            return None


def main() -> None:
    config = load_agent_config()
    agent = WifiUploadAgent(config)
    agent.run_forever()


if __name__ == "__main__":
    main()
from __future__ import annotations

import hashlib
import json
import random
import time
from datetime import datetime, timezone
from typing import Any

from app.hardware_agent.client import WifiApiClient, WifiUploadError
from app.hardware_agent.config import AgentConfig, load_agent_config
from app.hardware_agent.scanner import WifiNetwork, WifiScanner, WifiScannerError
from app.hardware_agent.storage import JsonFileStorage, QueueItem
from app.utils.logger import get_logger


logger = get_logger(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class WifiUploadAgent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.scanner = WifiScanner(config.interface)
        self.storage = JsonFileStorage(config.state_file, config.queue_file)
        self.client = WifiApiClient(
            endpoint_url=config.endpoint_url,
            timeout_seconds=config.request_timeout_seconds,
        )

    def run_forever(self) -> None:
        logger.info(
            "Starting WiFi upload agent for device_uuid=%s device_id=%s interval=%ss heartbeat=%ss endpoint=%s",
            self.config.device_uuid,
            self.config.device_id or "<unknown>",
            self.config.scan_interval_seconds,
            self.config.heartbeat_seconds,
            self.config.endpoint_url,
        )
        while True:
            loop_started = time.monotonic()
            try:
                self.run_once()
            except Exception:
                logger.exception("WiFi upload agent loop failed unexpectedly")
            sleep_for = max(1, self.config.scan_interval_seconds - int(time.monotonic() - loop_started))
            time.sleep(sleep_for)

    def run_once(self) -> None:
        self._flush_queue()

        try:
            networks = self.scanner.scan()
        except WifiScannerError:
            logger.exception("WiFi scan failed")
            return

        logger.info("WiFi scan success: found %s networks", len(networks))
        payload = self._build_payload(networks)
        state = self.storage.load_state()
        current_signature = self._signature(payload["wifi_networks"])
        last_signature = state.get("last_sent_signature")
        last_sent_at = self._parse_epoch(state.get("last_sent_at_epoch"))
        now = time.time()

        has_changed = current_signature != last_signature
        heartbeat_due = last_sent_at is None or (now - last_sent_at) >= self.config.heartbeat_seconds

        if not has_changed and not heartbeat_due:
            logger.info("WiFi scan unchanged and heartbeat not due; skipping upload")
            state["last_scan_signature"] = current_signature
            state["last_scan_at"] = utc_now_iso()
            self.storage.save_state(state)
            return

        reason = "change detected" if has_changed else "heartbeat due"
        logger.info("WiFi upload scheduled: %s", reason)
        if self._send_payload(payload):
            state["last_sent_signature"] = current_signature
            state["last_sent_at"] = payload["timestamp"]
            state["last_sent_at_epoch"] = now
            state["last_scan_signature"] = current_signature
            state["last_scan_at"] = payload["timestamp"]
            self.storage.save_state(state)
            return

        state["last_scan_signature"] = current_signature
        state["last_scan_at"] = payload["timestamp"]
        self.storage.save_state(state)
        if self._is_signature_buffered(current_signature):
            logger.warning("Offline mode active; identical WiFi payload already buffered")
            return
        self.storage.append_queue_item(QueueItem(payload=payload, retry_count=0))
        logger.warning("Offline mode activated; WiFi payload buffered locally")

    def _flush_queue(self) -> None:
        queue = self.storage.load_queue()
        if not queue:
            return

        logger.info("Attempting resend of %s buffered WiFi payload(s)", len(queue))
        remaining: list[QueueItem] = []
        for item in queue[: self.config.max_batch_size]:
            if self._send_payload(item.payload, retry_count=item.retry_count):
                self._mark_payload_sent(item.payload)
                continue
            remaining.append(QueueItem(payload=item.payload, retry_count=item.retry_count + 1))
            remaining.extend(queue[self.config.max_batch_size :])
            self.storage.save_queue(remaining)
            logger.warning("Buffered resend paused after failure; %s payload(s) remain", len(remaining))
            return

        remaining.extend(queue[self.config.max_batch_size :])
        self.storage.save_queue(remaining)
        if not remaining:
            logger.info("Buffered WiFi queue drained successfully")

    def _send_payload(self, payload: dict[str, Any], retry_count: int = 0) -> bool:
        max_attempts = self.config.retry_max_attempts
        for attempt in range(1, max_attempts + 1):
            try:
                self.client.send_scan(payload)
                logger.info(
                    "WiFi upload success: networks=%s timestamp=%s",
                    len(payload["wifi_networks"]),
                    payload["timestamp"],
                )
                return True
            except WifiUploadError as exc:
                logger.warning(
                    "WiFi upload failed on attempt %s/%s: %s",
                    attempt,
                    max_attempts,
                    exc,
                )
                if attempt == max_attempts:
                    break
                delay = self._backoff_delay(retry_count + attempt - 1)
                logger.info("Retrying WiFi upload in %.1f seconds", delay)
                time.sleep(delay)
        return False

    def _build_payload(self, networks: list[WifiNetwork]) -> dict[str, Any]:
        return {
            "wifi_networks": [network.to_payload() for network in networks],
            "timestamp": utc_now_iso(),
        }

    def _backoff_delay(self, retry_count: int) -> float:
        cap = self.config.max_retry_delay_seconds
        base_delay = min(cap, self.config.retry_base_delay_seconds * (2 ** retry_count))
        return min(cap, base_delay + random.uniform(0, 0.5))

    def _mark_payload_sent(self, payload: dict[str, Any]) -> None:
        state = self.storage.load_state()
        signature = self._signature(payload["wifi_networks"])
        state["last_sent_signature"] = signature
        state["last_sent_at"] = payload["timestamp"]
        state["last_sent_at_epoch"] = time.time()
        self.storage.save_state(state)

    def _is_signature_buffered(self, signature: str) -> bool:
        queue = self.storage.load_queue()
        for item in queue:
            queued_signature = self._signature(item.payload.get("wifi_networks", []))
            if queued_signature == signature:
                return True
        return False

    @staticmethod
    def _signature(networks: list[dict[str, Any]]) -> str:
        canonical = json.dumps(networks, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_epoch(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def main() -> None:
    config = load_agent_config()
    agent = WifiUploadAgent(config)
    agent.run_forever()


if __name__ == "__main__":
    main()

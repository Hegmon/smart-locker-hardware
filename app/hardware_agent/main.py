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
        logger.info(
            "Starting WiFi upload agent for device_uuid=%s device_id=%s scan_url=%s state_url=%s command_url=%s",
            self.config.device_uuid,
            self.config.device_id or "<unknown>",
            self.config.scan_event_url,
            self.config.state_update_url or "<disabled>",
            self.config.command_poll_url or "<disabled>",
        )
        while True:
            loop_started = time.monotonic()
            try:
                self.run_once()
            except Exception:
                logger.exception("WiFi upload agent loop failed unexpectedly")
            elapsed = time.monotonic() - loop_started
            print(f"[agent] loop completed in {elapsed:.1f}s, sleeping {max(1, self.config.scan_interval_seconds - int(elapsed))}s", flush=True)
            time.sleep(max(1, self.config.scan_interval_seconds - int(elapsed)))

    def run_once(self) -> None:
        self._flush_queue()

        connected_state = self._get_connected_state()

        print("[agent] Starting WiFi scan", flush=True)
        try:
            networks = self.scanner.scan()
        except WifiScannerError as exc:
            print(f"[agent] WiFi scan failed: {exc}", flush=True)
            logger.exception("WiFi scan failed")
        else:
            print(f"[agent] WiFi scan success: found {len(networks)} networks", flush=True)
            for net in networks:
                print(f"[agent] Network: {net.ssid} rssi={net.rssi} secured={net.is_secured}", flush=True)
            logger.info("WiFi scan success: found %s networks", len(networks))
            self._maybe_report_scan_diff(networks, connected_state)

        if self.config.command_poll_url:
            self._poll_and_execute_command()

    def _maybe_report_scan_diff(self, networks: list[WifiNetwork], connected_state: dict[str, Any]) -> None:
        state = self.storage.load_state()
        previous_networks = self._extract_network_map(state.get("last_scan_networks"))
        current_networks = {network.ssid: network for network in networks if network.ssid}
        diff_payload = self._build_scan_diff_payload(previous_networks, current_networks, connected_state)
        has_diff = bool(
            diff_payload["new_networks"]
            or diff_payload["removed_networks"]
            or diff_payload["updated_networks"]
        )
        last_sent_at = self._parse_epoch(state.get("last_scan_reported_at_epoch"))
        heartbeat_due = last_sent_at is None or (time.time() - last_sent_at) >= self.config.heartbeat_seconds
        connection_signature = self._signature(
            {
                "connected_ssid": connected_state["connected_ssid"],
                "signal_strength": connected_state["signal_strength"],
                "status": connected_state["status"],
            }
        )
        last_connection_signature = str(state.get("last_connection_signature") or "")
        connection_changed = connection_signature != last_connection_signature

        state["last_scan_networks"] = {ssid: network.to_payload() for ssid, network in current_networks.items()}
        state["last_scan_at"] = diff_payload["timestamp"]
        self.storage.save_state(state)

        if not has_diff and not heartbeat_due and not connection_changed:
            print("[agent] No changes detected; skipping upload", flush=True)
            logger.info("WiFi scan unchanged and connection state unchanged; skipping upload")
            return

        if not has_diff and (heartbeat_due or connection_changed):
            print("[agent] Heartbeat or connection change; sending empty scan event", flush=True)
            logger.info("WiFi diff empty but heartbeat or connection change requires upload")

        print(f"[agent] Sending scan event to {self.config.scan_event_url}", flush=True)
        print(f"[agent] Payload: {diff_payload}", flush=True)
        if self._send_with_retry("scan_event", self.config.scan_event_url, diff_payload):
            state = self.storage.load_state()
            state["last_scan_reported_at"] = diff_payload["timestamp"]
            state["last_scan_reported_at_epoch"] = time.time()
            state["last_connection_signature"] = connection_signature
            self.storage.save_state(state)
            return

        self.storage.append_queue_item(QueueItem(kind="scan_event", payload=diff_payload, retry_count=0))
        logger.warning("WiFi scan event buffered locally after upload failure")

    def _maybe_report_state(self, connected_state: dict[str, Any], force: bool = False) -> None:
        if not self.config.state_update_url:
            return
        payload = self._build_state_payload(connected_state)
        signature = self._signature(
            {
                "device_uuid": payload["device_uuid"],
                "device_id": payload["device_id"],
                "connected_ssid": payload["connected_ssid"],
                "signal_strength": payload["signal_strength"],
                "rssi": payload["rssi"],
                "status": payload["status"],
            }
        )
        state = self.storage.load_state()
        last_signature = str(state.get("last_state_signature") or "")
        last_report_at = self._parse_epoch(state.get("last_state_reported_at_epoch"))
        heartbeat_due = last_report_at is None or (
            time.time() - last_report_at
        ) >= self.config.state_heartbeat_seconds

        if not force and signature == last_signature and not heartbeat_due:
            return

        if self._send_with_retry("state_update", self.config.state_update_url, payload):
            state = self.storage.load_state()
            state["last_state_signature"] = signature
            state["last_state_reported_at"] = payload["timestamp"]
            state["last_state_reported_at_epoch"] = time.time()
            self.storage.save_state(state)
            logger.info(
                "WiFi state reported: status=%s connected_ssid=%s",
                payload["status"],
                payload["connected_ssid"],
            )
            return

        self.storage.append_queue_item(QueueItem(kind="state_update", payload=payload, retry_count=0))
        logger.warning("WiFi state update buffered locally after upload failure")

    def _poll_and_execute_command(self) -> None:
        if not self.config.command_poll_url:
            return
        state = self.storage.load_state()
        last_polled_at = self._parse_epoch(state.get("last_command_poll_at_epoch"))
        if last_polled_at is not None and (
            time.time() - last_polled_at
        ) < self.config.command_poll_interval_seconds:
            return

        state["last_command_poll_at_epoch"] = time.time()
        state["last_command_poll_at"] = utc_now_iso()
        self.storage.save_state(state)

        try:
            command = self.client.get_json(self.config.command_poll_url)
        except WifiApiError as exc:
            logger.warning("WiFi command poll failed: %s", exc)
            return

        if not command:
            return

        command_id = str(command.get("id") or command.get("command_id") or "").strip()
        if command_id and command_id == str(state.get("last_completed_command_id") or ""):
            logger.info("Skipping already completed WiFi command id=%s", command_id)
            return

        result = self._execute_command(command)
        if command_id:
            state = self.storage.load_state()
            state["last_completed_command_id"] = command_id
            self.storage.save_state(state)
            self._report_command_result(command_id, result)

    def _execute_command(self, command: dict[str, Any]) -> dict[str, Any]:
        command_type = str(command.get("command_type") or "").strip().upper()
        timestamp = utc_now_iso()

        if command_type != "CONNECT_WIFI":
            logger.warning("Unsupported WiFi command received: %s", command_type or "<empty>")
            return {
                "command_type": command_type,
                "status": "UNSUPPORTED",
                "message": "Unsupported WiFi command type",
                "timestamp": timestamp,
            }

        ssid = str(command.get("ssid") or "").strip()
        password = str(command.get("password") or "")
        if not ssid:
            logger.warning("CONNECT_WIFI command rejected because ssid was empty")
            return {
                "command_type": command_type,
                "status": "FAILED",
                "message": "Missing ssid",
                "timestamp": timestamp,
            }

        logger.info("Executing WiFi command CONNECT_WIFI for ssid=%s", ssid)
        try:
            connect_result = connect_wifi(ssid, password)
            connected_state = self._get_connected_state()
            connected_ssid = str(connected_state.get("connected_ssid") or "")
            if connected_ssid != ssid:
                raise WifiCommandError(f"connection verification failed, active ssid is {connected_ssid or '<none>'}")
        except WifiCommandError as exc:
            logger.warning("WiFi connect command failed for ssid=%s: %s", ssid, exc)
            result = {
                "command_type": command_type,
                "status": "FAILED",
                "ssid": ssid,
                "message": str(exc),
                "timestamp": utc_now_iso(),
            }
            self._maybe_report_state(self._get_connected_state(), force=True)
            return result

        connected_state = self._get_connected_state()
        self._maybe_report_state(connected_state, force=True)
        return {
            "command_type": command_type,
            "status": "CONNECTED",
            "ssid": ssid,
            "timestamp": utc_now_iso(),
            "connection": connected_state,
            "details": connect_result.get("details", ""),
        }

    def _report_command_result(self, command_id: str, result: dict[str, Any]) -> None:
        if not self.config.command_result_url_template:
            logger.info("Skipping WiFi command result callback because no result endpoint is configured")
            return
        url = self.config.command_result_url_template.format(command_id=command_id)
        if self._send_with_retry("command_result", url, result):
            logger.info("Reported WiFi command result for command_id=%s", command_id)
            return
        self.storage.append_queue_item(QueueItem(kind="command_result", payload={"url": url, "body": result}, retry_count=0))
        logger.warning("Buffered WiFi command result locally for command_id=%s", command_id)

    def _flush_queue(self) -> None:
        queue = self.storage.load_queue()
        if not queue:
            return

        logger.info("Attempting resend of %s buffered WiFi payload(s)", len(queue))
        remaining: list[QueueItem] = []
        for item in queue[: self.config.max_batch_size]:
            if self._deliver_queue_item(item):
                continue
            remaining.append(QueueItem(kind=item.kind, payload=item.payload, retry_count=item.retry_count + 1))
            remaining.extend(queue[self.config.max_batch_size :])
            self.storage.save_queue(remaining)
            logger.warning("Buffered resend paused after failure; %s payload(s) remain", len(remaining))
            return

        remaining.extend(queue[self.config.max_batch_size :])
        self.storage.save_queue(remaining)
        if not remaining:
            logger.info("Buffered WiFi queue drained successfully")

    def _deliver_queue_item(self, item: QueueItem) -> bool:
        if item.kind == "scan_event":
            return self._send_with_retry(item.kind, self.config.scan_event_url, item.payload, item.retry_count)
        if item.kind == "state_update":
            return self._send_with_retry(item.kind, self.config.state_update_url, item.payload, item.retry_count)
        if item.kind == "command_result":
            url = str(item.payload.get("url") or "").strip()
            body = item.payload.get("body")
            if not url or not isinstance(body, dict):
                return True
            return self._send_with_retry(item.kind, url, body, item.retry_count)
        return True

    def _send_with_retry(self, kind: str, url: str, payload: dict[str, Any], retry_count: int = 0) -> bool:
        max_attempts = self.config.retry_max_attempts
        for attempt in range(1, max_attempts + 1):
            try:
                self.client.post_json(url, payload)
                logger.info("%s upload success", kind)
                return True
            except WifiApiError as exc:
                logger.warning("%s upload failed on attempt %s/%s: %s", kind, attempt, max_attempts, exc)
                if attempt == max_attempts:
                    break
                delay = self._backoff_delay(retry_count + attempt - 1)
                logger.info("Retrying %s in %.1f seconds", kind, delay)
                time.sleep(delay)
        return False

    def _build_scan_diff_payload(
        self,
        previous_networks: dict[str, WifiNetwork],
        current_networks: dict[str, WifiNetwork],
        connected_state: dict[str, Any],
    ) -> dict[str, Any]:
        new_networks = [
            network.to_payload()
            for ssid, network in sorted(current_networks.items())
            if ssid not in previous_networks
        ]
        removed_networks = [
            previous_networks[ssid].to_payload()
            for ssid in sorted(previous_networks)
            if ssid not in current_networks
        ]
        updated_networks = []
        for ssid in sorted(current_networks):
            previous = previous_networks.get(ssid)
            current = current_networks[ssid]
            if previous is None:
                continue
            signal_changed = abs(current.rssi - previous.rssi) >= self.config.signal_change_threshold
            security_changed = current.is_secured != previous.is_secured
            if signal_changed or security_changed:
                updated_networks.append(
                    {
                        "ssid": current.ssid,
                        "previous_rssi": previous.rssi,
                        "rssi": current.rssi,
                        "previous_is_secured": previous.is_secured,
                        "is_secured": current.is_secured,
                    }
                )

        return {
            "device_uuid": self.config.device_uuid,
            "device_id": self.config.device_id,
            "connected_ssid": connected_state["connected_ssid"],
            "signal_strength": connected_state["signal_strength"],
            "timestamp": utc_now_iso(),
            "new_networks": new_networks,
            "removed_networks": removed_networks,
            "updated_networks": updated_networks,
        }

    def _build_state_payload(self, connected_state: dict[str, Any]) -> dict[str, Any]:
        return {
            "device_uuid": self.config.device_uuid,
            "device_id": self.config.device_id,
            "connected_ssid": connected_state["connected_ssid"],
            "signal_strength": connected_state["signal_strength"],
            "rssi": connected_state["rssi"],
            "status": connected_state["status"],
            "timestamp": utc_now_iso(),
        }

    @staticmethod
    def _extract_network_map(raw_networks: Any) -> dict[str, WifiNetwork]:
        if not isinstance(raw_networks, dict):
            return {}
        networks: dict[str, WifiNetwork] = {}
        for ssid, payload in raw_networks.items():
            if not isinstance(payload, dict):
                continue
            networks[str(ssid)] = WifiNetwork(
                ssid=str(payload.get("ssid") or ssid),
                rssi=WifiUploadAgent._safe_int(payload.get("rssi"), -100),
                is_secured=bool(payload.get("is_secured", False)),
            )
        return networks

    @staticmethod
    def _get_connected_state() -> dict[str, Any]:
        details = get_connected_wifi_details()
        return {
            "connected_ssid": str(details.get("connected_ssid") or ""),
            "signal_strength": WifiUploadAgent._safe_int(details.get("signal_strength"), 0),
            "rssi": WifiUploadAgent._safe_int(details.get("rssi"), -100),
            "status": "CONNECTED" if details.get("connected") else "DISCONNECTED",
        }

    def _backoff_delay(self, retry_count: int) -> float:
        cap = self.config.max_retry_delay_seconds
        base_delay = min(cap, self.config.retry_base_delay_seconds * (2 ** retry_count))
        return min(cap, base_delay + random.uniform(0, 0.5))

    @staticmethod
    def _signature(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_epoch(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default


def main() -> None:
    import os
    config = load_agent_config()
    agent = WifiUploadAgent(config)
    if os.getenv("RUN_ONCE", "").lower() in ("1", "true", "yes"):
        agent.run_once()
    else:
        agent.run_forever()


if __name__ == "__main__":
    main()

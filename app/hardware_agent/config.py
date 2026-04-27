from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.config import (
    QBOX_WIFI_AGENT_BASE_URL,
    QBOX_WIFI_AGENT_DEVICE_ID,
    QBOX_WIFI_AGENT_HEARTBEAT_SECONDS,
    QBOX_WIFI_AGENT_MAX_BATCH_SIZE,
    QBOX_WIFI_AGENT_MAX_RETRY_DELAY_SECONDS,
    QBOX_WIFI_AGENT_QUEUE_FILE,
    QBOX_WIFI_AGENT_REQUEST_TIMEOUT_SECONDS,
    QBOX_WIFI_AGENT_RETRY_BASE_DELAY_SECONDS,
    QBOX_WIFI_AGENT_RETRY_MAX_ATTEMPTS,
    QBOX_WIFI_AGENT_SCAN_INTERVAL_SECONDS,
    QBOX_WIFI_AGENT_STATE_FILE,
    WIFI_INTERFACE,
)
from app.services.backend_state import load_backend_state


def _clamp(value: int, minimum: int) -> int:
    return max(value, minimum)


@dataclass(frozen=True)
class AgentConfig:
    base_url: str
    device_uuid: str
    device_id: str
    interface: str
    scan_interval_seconds: int
    heartbeat_seconds: int
    request_timeout_seconds: float
    retry_max_attempts: int
    retry_base_delay_seconds: float
    max_batch_size: int
    max_retry_delay_seconds: int
    state_file: Path
    queue_file: Path

    @property
    def endpoint_url(self) -> str:
        return f"{self.base_url}/devices/{self.device_uuid}/wifi/"


def load_agent_config() -> AgentConfig:
    backend_state = load_backend_state()
    resolved_device_uuid = (
        str(backend_state.get("device_uuid") or "").strip()
        or QBOX_WIFI_AGENT_DEVICE_ID
        or str(backend_state.get("device_id") or "").strip()
    )
    resolved_device_id = str(backend_state.get("device_id") or "").strip()

    if not resolved_device_uuid:
        raise RuntimeError(
            "WiFi agent device UUID is not configured. Register the device so "
            "app/config/backend_device.json contains device_uuid, or set "
            "QBOX_WIFI_AGENT_DEVICE_ID to the backend UUID explicitly."
        )

    return AgentConfig(
        base_url=QBOX_WIFI_AGENT_BASE_URL,
        device_uuid=resolved_device_uuid,
        device_id=resolved_device_id,
        interface=WIFI_INTERFACE,
        scan_interval_seconds=_clamp(QBOX_WIFI_AGENT_SCAN_INTERVAL_SECONDS, 15),
        heartbeat_seconds=_clamp(QBOX_WIFI_AGENT_HEARTBEAT_SECONDS, 60),
        request_timeout_seconds=max(QBOX_WIFI_AGENT_REQUEST_TIMEOUT_SECONDS, 1.0),
        retry_max_attempts=_clamp(QBOX_WIFI_AGENT_RETRY_MAX_ATTEMPTS, 1),
        retry_base_delay_seconds=max(QBOX_WIFI_AGENT_RETRY_BASE_DELAY_SECONDS, 1.0),
        max_batch_size=_clamp(QBOX_WIFI_AGENT_MAX_BATCH_SIZE, 1),
        max_retry_delay_seconds=_clamp(QBOX_WIFI_AGENT_MAX_RETRY_DELAY_SECONDS, 30),
        state_file=Path(QBOX_WIFI_AGENT_STATE_FILE),
        queue_file=Path(QBOX_WIFI_AGENT_QUEUE_FILE),
    )

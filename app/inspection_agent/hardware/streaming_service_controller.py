from __future__ import annotations

"""Best-effort systemd control for the streaming runtime during camera tests."""

import subprocess
import time
from dataclasses import dataclass

from app.deployment.runtime_config import get_str_setting
from app.utils.logger import get_logger


logger = get_logger(__name__)


DEFAULT_STREAMING_SERVICE_NAMES = (
    "qbox-device.service",
    "qbox-streaming-agent.service",
    "smartlocker-streaming-agent.service",
    "qbox-streams.service",
)


@dataclass(frozen=True)
class StreamingServiceControlResult:
    """Records which services were stopped so they can be restored later."""

    stopped_services: tuple[str, ...]


class StreamingServiceController:
    """Stops and restarts streaming-related services around camera inspection."""

    def __init__(self) -> None:
        configured_services = get_str_setting(
            "INSPECTION_STREAMING_SERVICE_NAMES",
            ",".join(DEFAULT_STREAMING_SERVICE_NAMES),
        )
        self.service_names = tuple(
            service.strip()
            for service in configured_services.split(",")
            if service.strip()
        )

    def stop_streaming_services(self, *, timeout_seconds: float = 12.0) -> StreamingServiceControlResult:
        stopped: list[str] = []
        for service in self.service_names:
            if not self._is_active(service):
                continue
            if self._systemctl("stop", service):
                stopped.append(service)
                self._wait_for_state(service, active=False, timeout_seconds=timeout_seconds)
        if stopped:
            logger.info("Temporarily stopped streaming services for inspection: %s", stopped)
        return StreamingServiceControlResult(stopped_services=tuple(stopped))

    def start_streaming_services(self, services: tuple[str, ...], *, timeout_seconds: float = 12.0) -> None:
        for service in services:
            if self._systemctl("start", service):
                self._wait_for_state(service, active=True, timeout_seconds=timeout_seconds)
        if services:
            logger.info("Restored streaming services after inspection: %s", services)

    def _systemctl(self, action: str, service: str) -> bool:
        try:
            result = subprocess.run(
                ["systemctl", action, service],
                capture_output=True,
                text=True,
                check=False,
                timeout=15.0,
            )
        except Exception as exc:
            logger.warning("systemctl %s failed for %s: %s", action, service, exc)
            return False

        if result.returncode != 0:
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            logger.warning(
                "systemctl %s returned rc=%s for %s stdout=%s stderr=%s",
                action,
                result.returncode,
                service,
                stdout,
                stderr,
            )
            return False
        return True

    def _is_active(self, service: str) -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", service],
                capture_output=True,
                text=True,
                check=False,
                timeout=5.0,
            )
            return result.returncode == 0
        except Exception:
            logger.debug("systemctl is-active failed for %s", service, exc_info=True)
            return False

    def _wait_for_state(self, service: str, *, active: bool, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while time.monotonic() < deadline:
            if self._is_active(service) == active:
                return True
            time.sleep(0.25)
        logger.warning("Timed out waiting for %s to become %s", service, "active" if active else "inactive")
        return False

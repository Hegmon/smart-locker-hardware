from __future__ import annotations

import threading
import time

from app.streaming_agent.config.runtime import RelayConfig
from app.streaming_agent.event_bus.detection_events import DetectionEvent, DetectionType
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)


class SecurityRelayManager:
    """Authoritative ON/OFF owner for Relay 1 + Relay 4 security behavior."""

    def __init__(self, relay_controller, *, config: RelayConfig | None = None) -> None:
        self.relay_controller = relay_controller
        self.config = config or RelayConfig.from_env()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._active_until = 0.0
        self._relays_active = False
        self._last_event_key_at: dict[tuple[str, str], float] = {}
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="security-relay-manager")
        self._worker.start()

    def handle_detection_event(self, event: DetectionEvent) -> bool:
        if not self._should_activate(event):
            logger.info(
                "Detection ignored by relay policy camera=%s detection=%s confidence=%.2f reason=%s",
                event.camera_type,
                event.detection_type,
                event.confidence,
                event.reason or "n/a",
            )
            return False

        now = time.monotonic()
        event_key = (event.camera_type, event.detection_type)
        with self._condition:
            last_seen = self._last_event_key_at.get(event_key, 0.0)
            if self.config.cooldown_seconds > 0 and now - last_seen < self.config.cooldown_seconds:
                logger.info(
                    "Detection cooldown suppression camera=%s detection=%s age=%.3fs cooldown=%.3fs",
                    event.camera_type,
                    event.detection_type,
                    now - last_seen,
                    self.config.cooldown_seconds,
                )
                return False
            self._last_event_key_at[event_key] = now
            previous_until = self._active_until
            self._active_until = max(self._active_until, now + self.config.timeout_seconds)
            logger.info(
                "Detection accepted event_id=%s camera=%s detection=%s confidence=%.2f timer_until=%.3f previous_until=%.3f",
                event.event_id,
                event.camera_type,
                event.detection_type,
                event.confidence,
                self._active_until,
                previous_until,
            )
            if not self._relays_active:
                self._set_relays_locked(True, cause=f"{event.camera_type}:{event.detection_type}")
            else:
                logger.info(
                    "Relay timer reset camera=%s detection=%s remaining=%.2fs",
                    event.camera_type,
                    event.detection_type,
                    max(0.0, self._active_until - now),
                )
            self._condition.notify_all()
        return True

    def force_off(self, *, reason: str = "manual") -> None:
        with self._condition:
            self._active_until = 0.0
            self._set_relays_locked(False, cause=reason)
            self._condition.notify_all()

    def is_active(self) -> bool:
        with self._lock:
            return self._relays_active

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while self._active_until <= time.monotonic():
                    if self._relays_active:
                        self._set_relays_locked(False, cause="timeout")
                    self._condition.wait(timeout=0.5)
                wait_time = max(0.0, self._active_until - time.monotonic())
                self._condition.wait(timeout=wait_time)

    def _set_relays_locked(self, active: bool, *, cause: str) -> None:
        if self._relays_active == active:
            return
        desired = "ON" if active else "OFF"
        for attempt in range(1, self.config.retry_count + 1):
            try:
                self.relay_controller.set_security_relays(active)
                self._relays_active = active
                logger.info(
                    "Relay command %s cause=%s attempt=%s/%s",
                    desired,
                    cause,
                    attempt,
                    self.config.retry_count,
                )
                return
            except Exception:
                logger.exception(
                    "Relay command failed desired=%s cause=%s attempt=%s/%s",
                    desired,
                    cause,
                    attempt,
                    self.config.retry_count,
                )
                if attempt < self.config.retry_count:
                    time.sleep(self.config.retry_delay_seconds)
        if not active and hasattr(self.relay_controller, "force_security_relays_off"):
            try:
                self.relay_controller.force_security_relays_off()
                self._relays_active = False
                logger.warning("Relay OFF recovery used cause=%s", cause)
            except Exception:
                logger.exception("Relay OFF recovery failed cause=%s", cause)

    @staticmethod
    def _should_activate(event: DetectionEvent) -> bool:
        detection_type = str(event.detection_type)
        camera_type = str(event.camera_type)
        if camera_type == "internal" and detection_type in {
            DetectionType.PERSON_DETECTED.value,
            DetectionType.MOTION_DETECTED.value,
            DetectionType.TAMPER_DETECTED.value,
        }:
            return True
        return camera_type == "external" and detection_type == DetectionType.TAMPER_DETECTED.value

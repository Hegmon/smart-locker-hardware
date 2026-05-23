from __future__ import annotations

import threading
import time

from app.streaming_agent.config.runtime import RelayConfig
from app.streaming_agent.event_bus.detection_events import DetectionEvent, DetectionType
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)


class SecurityRelayManager:
    """Authoritative lifecycle owner for Relay 1 + Relay 4 security behavior.

    The relay pair stays ON while any supported detection is active. Once all
    detections clear, a single OFF deadline is scheduled. No detector is allowed
    to spam timer extensions frame-by-frame.
    """

    def __init__(self, relay_controller, *, config: RelayConfig | None = None) -> None:
        self.relay_controller = relay_controller
        self.config = config or RelayConfig.from_env()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._relay_session_active = False
        self._off_deadline: float | None = None
        self._active_detections = {
            "internal_person": False,
            "internal_motion": False,
            "internal_tamper": False,
            "external_tamper": False,
        }
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="security-relay-manager")
        self._worker.start()

    def handle_detection_event(self, event: DetectionEvent) -> bool:
        detection_type = str(event.detection_type)
        detection_key = self._event_to_state_key(event)
        if detection_key is None:
            logger.info(
                "Detection ignored by relay policy camera=%s detection=%s confidence=%.2f",
                event.camera_type,
                event.detection_type,
                event.confidence,
            )
            return False

        with self._condition:
            previous_value = self._active_detections[detection_key]
            new_value = detection_type.endswith("_DETECTED")
            if previous_value == new_value:
                return False

            self._active_detections[detection_key] = new_value
            logger.info(
                "Active detection state updated key=%s active=%s event_id=%s reason=%s",
                detection_key,
                new_value,
                event.event_id,
                event.reason or "n/a",
            )

            if new_value:
                self._off_deadline = None
                self._condition.notify_all()
            elif not self._any_detection_active_locked():
                self._off_deadline = time.monotonic() + self.config.timeout_seconds
                logger.info(
                    "All detections cleared; relay OFF timeout scheduled in %.2fs",
                    self.config.timeout_seconds,
                )
                self._condition.notify_all()
            return True

    def force_off(self, *, reason: str = "manual") -> None:
        with self._condition:
            for key in self._active_detections:
                self._active_detections[key] = False
            self._off_deadline = None
            self._set_relays_locked(False, cause=reason)
            self._condition.notify_all()

    def is_active(self) -> bool:
        with self._lock:
            return self._relay_session_active

    def active_snapshot(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._active_detections)

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                any_active = self._any_detection_active_locked()
                now = time.monotonic()

                if any_active:
                    if not self._relay_session_active:
                        self._set_relays_locked(True, cause="active_detection")
                        logger.info("Relay session activated")
                    self._off_deadline = None
                    self._condition.wait(timeout=0.1)
                    continue

                if self._relay_session_active and self._off_deadline is None:
                    self._off_deadline = now + self.config.timeout_seconds
                    logger.info(
                        "Relay OFF timeout scheduled in %.2fs",
                        self.config.timeout_seconds,
                    )

                if self._relay_session_active and self._off_deadline is not None and now >= self._off_deadline:
                    self._set_relays_locked(False, cause="timeout_expired")
                    self._off_deadline = None
                    logger.info("Relay OFF executed")
                    self._condition.wait(timeout=0.1)
                    continue

                sleep_for = 0.1
                if self._off_deadline is not None:
                    sleep_for = max(0.05, min(0.25, self._off_deadline - now))
                self._condition.wait(timeout=sleep_for)

    def _set_relays_locked(self, active: bool, *, cause: str) -> None:
        if self._relay_session_active == active:
            return
        desired = "ON" if active else "OFF"
        for attempt in range(1, self.config.retry_count + 1):
            try:
                self.relay_controller.set_security_relays(active)
                self._relay_session_active = active
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
                self._relay_session_active = False
                logger.warning("Relay OFF recovery used cause=%s", cause)
            except Exception:
                logger.exception("Relay OFF recovery failed cause=%s", cause)

    def _any_detection_active_locked(self) -> bool:
        return any(self._active_detections.values())

    @staticmethod
    def _event_to_state_key(event: DetectionEvent) -> str | None:
        camera = str(event.camera_type)
        detection = str(event.detection_type)
        mapping = {
            ("internal", DetectionType.PERSON_DETECTED.value): "internal_person",
            ("internal", DetectionType.PERSON_CLEARED.value): "internal_person",
            ("internal", DetectionType.MOTION_DETECTED.value): "internal_motion",
            ("internal", DetectionType.MOTION_CLEARED.value): "internal_motion",
            ("internal", DetectionType.TAMPER_DETECTED.value): "internal_tamper",
            ("internal", DetectionType.TAMPER_CLEARED.value): "internal_tamper",
            ("external", DetectionType.TAMPER_DETECTED.value): "external_tamper",
            ("external", DetectionType.TAMPER_CLEARED.value): "external_tamper",
        }
        return mapping.get((camera, detection))

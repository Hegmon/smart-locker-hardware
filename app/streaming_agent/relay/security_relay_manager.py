from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from app.streaming_agent.config.runtime import RelayConfig
from app.streaming_agent.event_bus.detection_events import DetectionEvent, DetectionType
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)


@dataclass
class SecurityRelayState:
    relay_active: bool = False
    last_detection_ts: float = 0.0
    off_deadline_ts: float = 0.0
    active_detection_sources: set[str] = field(default_factory=set)
    last_event_by_source: dict[str, float] = field(default_factory=dict)


class SecurityRelayManager:
    """Single deadline-driven relay control loop for Relay 1 + Relay 4."""

    def __init__(self, relay_controller, *, config: RelayConfig | None = None) -> None:
        self.relay_controller = relay_controller
        self.config = config or RelayConfig.from_env()
        self.state = SecurityRelayState()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="security-relay-manager")
        self._worker.start()

    def handle_detection_event(self, event: DetectionEvent) -> bool:
        source_key = self._event_to_source_key(event)
        detection_type = str(event.detection_type)
        logger.info("DETECTION RECEIVED: camera=%s type=%s", event.camera_type, detection_type)
        if source_key is None:
            logger.info("DETECTION IGNORED: reason=unsupported camera=%s type=%s", event.camera_type, detection_type)
            return False

        now = time.monotonic()
        with self._condition:
            if detection_type.endswith("_CLEARED"):
                self.state.active_detection_sources.discard(source_key)
                self.state.last_event_by_source.pop(source_key, None)
                logger.info(
                    "DETECTION CLEARED: source=%s active_sources=%s",
                    source_key,
                    sorted(self.state.active_detection_sources),
                )
                self._condition.notify_all()
                return True

            last_seen = self.state.last_event_by_source.get(source_key, 0.0)
            age = now - last_seen
            if last_seen and age < self.config.detection_debounce_seconds:
                logger.info("DETECTION IGNORED: reason=debounce source=%s age=%.2fs", source_key, age)
                return False

            self.state.last_event_by_source[source_key] = now
            self.state.last_detection_ts = now
            self.state.off_deadline_ts = now + self.config.timeout_seconds
            self.state.active_detection_sources.add(source_key)
            logger.info("DETECTION ACCEPTED: new_off_deadline=%.2f source=%s", self.state.off_deadline_ts, source_key)
            logger.info(
                "RELAY STATE: active=%s deadline=%.2f active_sources=%s",
                self.state.relay_active,
                self.state.off_deadline_ts,
                sorted(self.state.active_detection_sources),
            )
            if not self.state.relay_active:
                self._set_relays_locked(True, reason="detection_accepted")
            self._condition.notify_all()
            return True

    def force_off(self, *, reason: str = "manual") -> None:
        with self._condition:
            self.state.active_detection_sources.clear()
            self.state.last_event_by_source.clear()
            self.state.last_detection_ts = 0.0
            self.state.off_deadline_ts = 0.0
            self._set_relays_locked(False, reason=reason)
            self._condition.notify_all()

    def is_active(self) -> bool:
        with self._lock:
            return self.state.relay_active

    def active_snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "relay_active": self.state.relay_active,
                "last_detection_ts": self.state.last_detection_ts,
                "off_deadline_ts": self.state.off_deadline_ts,
                "active_detection_sources": sorted(self.state.active_detection_sources),
            }

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                now = time.monotonic()
                if self.state.relay_active and self.state.off_deadline_ts > 0:
                    remaining = max(0.0, self.state.off_deadline_ts - now)
                    logger.info("OFF CHECK: remaining=%.2fs", remaining)
                    if now >= self.state.off_deadline_ts:
                        self._set_relays_locked(False, reason="timeout_expired")
                        self.state.active_detection_sources.clear()
                        self.state.last_event_by_source.clear()
                        self.state.last_detection_ts = 0.0
                        self.state.off_deadline_ts = 0.0
                        logger.info("RELAY STATE: active=%s deadline=%.2f", self.state.relay_active, self.state.off_deadline_ts)
                        self._condition.wait(timeout=0.1)
                        continue
                self._condition.wait(timeout=0.1)

    def _set_relays_locked(self, active: bool, *, reason: str) -> None:
        if self.state.relay_active == active:
            return
        for attempt in range(1, self.config.retry_count + 1):
            try:
                self.relay_controller.set_security_relays(active)
            except Exception:
                logger.exception(
                    "Relay command failed desired=%s attempt=%s/%s",
                    "ON" if active else "OFF",
                    attempt,
                    self.config.retry_count,
                )
                time.sleep(self.config.retry_delay_seconds)
                continue
            if active:
                self.state.relay_active = True
                logger.info("RELAYS ON: relay1=True relay4=True attempt=%s/%s", attempt, self.config.retry_count)
                return
            if self._verify_relays_off():
                self.state.relay_active = False
                logger.info("RELAYS OFF: reason=%s attempt=%s/%s", reason, attempt, self.config.retry_count)
                return
            logger.warning("Relay OFF verification failed attempt=%s/%s", attempt, self.config.retry_count)
            time.sleep(self.config.retry_delay_seconds)

        self.relay_controller.force_security_relays_off()
        if self._verify_relays_off():
            self.state.relay_active = False
            logger.info("RELAYS OFF: reason=%s attempt=force", reason)
            return
        logger.error("Relay OFF verification failed after retries reason=%s", reason)

    def _verify_relays_off(self) -> bool:
        try:
            if not hasattr(self.relay_controller, "is_security_relays_on"):
                return True
            return not bool(self.relay_controller.is_security_relays_on())
        except Exception:
            logger.exception("Relay OFF verification threw an exception")
            return False

    @staticmethod
    def _event_to_source_key(event: DetectionEvent) -> str | None:
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

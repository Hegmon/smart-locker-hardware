from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from itertools import count

from app.streaming_agent.config.runtime import RelayConfig
from app.streaming_agent.event_bus.detection_events import DetectionEvent, DetectionType
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)
_MANAGER_IDS = count(1)


class RelayLifecycleState(str, Enum):
    IDLE = "STATE_IDLE"
    ACTIVE = "STATE_ACTIVE"
    WAITING_OFF = "STATE_WAITING_OFF"


@dataclass
class SecurityRelayState:
    lifecycle_state: RelayLifecycleState = RelayLifecycleState.IDLE
    relay_active: bool = False
    last_detection_ts: float = 0.0
    off_deadline_ts: float = 0.0
    state_changed_ts: float = 0.0
    active_detection_sources: set[str] = field(default_factory=set)
    last_event_by_source: dict[str, float] = field(default_factory=dict)


class SecurityRelayManager:
    """Single source of truth for Relay 1 + Relay 4 security control."""

    _instance_lock = threading.RLock()
    _active_instance: "SecurityRelayManager | None" = None

    def __init__(self, relay_controller, *, config: RelayConfig | None = None) -> None:
        self.manager_id = next(_MANAGER_IDS)
        self.relay_controller = relay_controller
        self.config = config or RelayConfig.from_env()
        self.state = SecurityRelayState(state_changed_ts=time.monotonic())
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._running = True
        self._last_state_log_at = 0.0
        with self._instance_lock:
            previous = self.__class__._active_instance
            if previous is not None:
                logger.critical(
                    "Duplicate SecurityRelayManager detected: stopping previous manager_id=%s before starting manager_id=%s",
                    previous.manager_id,
                    self.manager_id,
                )
                previous.stop()
            self.__class__._active_instance = self
        self._worker = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name=f"security-relay-manager-{self.manager_id}",
        )
        self._worker.start()
        logger.info("SecurityRelayManager started manager_id=%s", self.manager_id)

    def stop(self) -> None:
        with self._condition:
            if not self._running:
                return
            self._running = False
            self._condition.notify_all()
        self._worker.join(timeout=2.0)
        with self._instance_lock:
            if self.__class__._active_instance is self:
                self.__class__._active_instance = None
        logger.info("SecurityRelayManager stopped manager_id=%s", self.manager_id)

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
                return self._handle_cleared_locked(source_key, now)
            return self._handle_detected_locked(source_key, now)

    def force_off(self, *, reason: str = "manual") -> None:
        with self._condition:
            self.state.active_detection_sources.clear()
            self.state.last_event_by_source.clear()
            self.state.last_detection_ts = 0.0
            self.state.off_deadline_ts = 0.0
            self._transition_state_locked(RelayLifecycleState.IDLE, reason=reason)
            self._execute_relay_write_locked(False, reason=reason, force=True)
            self._condition.notify_all()

    def is_active(self) -> bool:
        with self._lock:
            return self.state.relay_active

    def active_snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "lifecycle_state": self.state.lifecycle_state.value,
                "relay_active": self.state.relay_active,
                "last_detection_ts": self.state.last_detection_ts,
                "off_deadline_ts": self.state.off_deadline_ts,
                "active_detection_sources": sorted(self.state.active_detection_sources),
                "gpio_state": self._gpio_state(),
            }

    def _handle_detected_locked(self, source_key: str, now: float) -> bool:
        if source_key in self.state.active_detection_sources:
            last_seen = self.state.last_event_by_source.get(source_key, 0.0)
            age = now - last_seen
            if last_seen and age < self.config.detection_debounce_seconds:
                logger.info("DETECTION IGNORED: reason=debounce source=%s age=%.2fs", source_key, age)
                return False
            self.state.last_event_by_source[source_key] = now
            self.state.last_detection_ts = now
            self.state.off_deadline_ts = now + self.config.timeout_seconds
            logger.info("DETECTION ACCEPTED: source=%s refreshed_off_deadline=%.2f", source_key, self.state.off_deadline_ts)
            self._log_relay_decision_locked()
            return True

        last_seen = self.state.last_event_by_source.get(source_key, 0.0)
        age = now - last_seen
        if last_seen and age < self.config.detection_debounce_seconds:
            logger.info("DETECTION IGNORED: reason=debounce source=%s age=%.2fs", source_key, age)
            return False

        self.state.last_event_by_source[source_key] = now
        self.state.last_detection_ts = now
        self.state.active_detection_sources.add(source_key)
        self.state.off_deadline_ts = now + self.config.timeout_seconds
        self._transition_state_locked(RelayLifecycleState.ACTIVE, reason=f"detected:{source_key}")
        logger.info("DETECTION ACCEPTED: source=%s new_off_deadline=%.2f", source_key, self.state.off_deadline_ts)
        self._log_relay_decision_locked()
        self._execute_relay_write_locked(True, reason="detection_accepted")
        self._condition.notify_all()
        return True

    def _handle_cleared_locked(self, source_key: str, now: float) -> bool:
        was_active = source_key in self.state.active_detection_sources
        self.state.active_detection_sources.discard(source_key)
        self.state.last_event_by_source.pop(source_key, None)
        logger.info("DETECTION CLEARED: source=%s active_sources=%s", source_key, sorted(self.state.active_detection_sources))
        if was_active and self.state.active_detection_sources:
            self._transition_state_locked(RelayLifecycleState.ACTIVE, reason=f"remaining_active:{source_key}")
        elif was_active:
            self.state.off_deadline_ts = now + self.config.timeout_seconds
            if self.state.relay_active:
                self._transition_state_locked(RelayLifecycleState.WAITING_OFF, reason="all_detections_cleared")
            else:
                self.state.off_deadline_ts = 0.0
                self._transition_state_locked(RelayLifecycleState.IDLE, reason="all_detections_cleared_relays_already_off")
        self._log_relay_decision_locked()
        self._condition.notify_all()
        return True

    def _worker_loop(self) -> None:
        while True:
            try:
                with self._condition:
                    if not self._running:
                        return
                    now = time.monotonic()
                    self._log_periodic_state_locked(now)
                    self._run_failsafe_locked(now)

                    if self.state.lifecycle_state is RelayLifecycleState.ACTIVE:
                        if not self.state.relay_active:
                            self._execute_relay_write_locked(True, reason="active_state_recovery")
                    elif self.state.lifecycle_state is RelayLifecycleState.WAITING_OFF:
                        remaining = max(0.0, self.state.off_deadline_ts - now)
                        logger.info("RELAY DECISION: active_sources=%s should_turn_off=%s remaining=%.2fs", sorted(self.state.active_detection_sources), now >= self.state.off_deadline_ts, remaining)
                        if now >= self.state.off_deadline_ts:
                            logger.info("RELAY OFF EXECUTION START")
                            if self._execute_relay_write_locked(False, reason="timeout_expired"):
                                logger.info("RELAY OFF EXECUTION SUCCESS")
                                self.state.last_detection_ts = 0.0
                                self.state.off_deadline_ts = 0.0
                                self.state.active_detection_sources.clear()
                                self.state.last_event_by_source.clear()
                                self._transition_state_locked(RelayLifecycleState.IDLE, reason="timeout_expired")
                            else:
                                logger.error("RELAY OFF EXECUTION FAILED")
                    timeout = self.config.poll_interval_seconds
                    self._condition.wait(timeout=timeout)
            except Exception:
                logger.exception("Security relay worker failed; continuing")
                time.sleep(self.config.poll_interval_seconds)

    def _run_failsafe_locked(self, now: float) -> None:
        if self.state.active_detection_sources:
            return
        if not self.state.relay_active:
            return
        state_age = now - self.state.state_changed_ts
        if state_age < self.config.stale_on_failsafe_seconds:
            return
        logger.critical(
            "Relay failsafe triggered: lifecycle=%s relay_active=%s active_sources=%s gpio_state=%s",
            self.state.lifecycle_state.value,
            self.state.relay_active,
            sorted(self.state.active_detection_sources),
            self._gpio_state(),
        )
        self.state.off_deadline_ts = 0.0
        self._transition_state_locked(RelayLifecycleState.IDLE, reason="failsafe")
        self._execute_relay_write_locked(False, reason="failsafe_force_off", force=True)

    def _transition_state_locked(self, new_state: RelayLifecycleState, *, reason: str) -> None:
        old_state = self.state.lifecycle_state
        if old_state is new_state:
            return
        self.state.lifecycle_state = new_state
        self.state.state_changed_ts = time.monotonic()
        logger.info("RELAY STATE TRANSITION: %s -> %s reason=%s", old_state.value, new_state.value, reason)

    def _log_relay_decision_locked(self) -> None:
        logger.info(
            "RELAY DECISION: manager_id=%s active_sources=%s state=%s relay_active=%s deadline=%.2f",
            self.manager_id,
            sorted(self.state.active_detection_sources),
            self.state.lifecycle_state.value,
            self.state.relay_active,
            self.state.off_deadline_ts,
        )

    def _log_periodic_state_locked(self, now: float) -> None:
        if now - self._last_state_log_at < self.config.state_log_interval_seconds:
            return
        self._last_state_log_at = now
        logger.info(
            "RELAY STATE SNAPSHOT: manager_id=%s state=%s active_sources=%s relay_active=%s gpio_state=%s deadline=%.2f",
            self.manager_id,
            self.state.lifecycle_state.value,
            sorted(self.state.active_detection_sources),
            self.state.relay_active,
            self._gpio_state(),
            self.state.off_deadline_ts,
        )

    def _execute_relay_write_locked(self, active: bool, *, reason: str, force: bool = False) -> bool:
        desired = "ON" if active else "OFF"
        supports_verify = hasattr(self.relay_controller, "is_security_relays_on")
        for attempt in range(1, self.config.retry_count + 1):
            try:
                if active:
                    self.relay_controller.set_security_relays(True)
                elif force:
                    self.relay_controller.force_security_relays_off()
                else:
                    self.relay_controller.set_security_relays(False)
            except Exception:
                logger.exception("Relay command failed desired=%s attempt=%s/%s", desired, attempt, self.config.retry_count)
                time.sleep(self.config.retry_delay_seconds)
                continue

            gpio_state = self._gpio_state() if supports_verify else active
            if active and gpio_state:
                self.state.relay_active = True
                logger.info("RELAYS ON: relay1=True relay4=True attempt=%s/%s", attempt, self.config.retry_count)
                return True
            if (not active) and (not gpio_state):
                self.state.relay_active = False
                logger.info("RELAYS OFF: reason=%s attempt=%s/%s", reason, attempt, self.config.retry_count)
                return True

            logger.warning(
                "Relay verification mismatch desired=%s gpio_state=%s attempt=%s/%s",
                desired,
                gpio_state,
                attempt,
                self.config.retry_count,
            )
            time.sleep(self.config.retry_delay_seconds)

        if not active and not force:
            try:
                self.relay_controller.force_security_relays_off()
            except Exception:
                logger.exception("Relay force-off failed")
            gpio_state = self._gpio_state() if supports_verify else False
            if not gpio_state:
                self.state.relay_active = False
                logger.info("RELAYS OFF: reason=%s attempt=force", reason)
                return True

        logger.error("Relay write failed desired=%s reason=%s", desired, reason)
        return False

    def _gpio_state(self) -> bool:
        try:
            if hasattr(self.relay_controller, "is_security_relays_on"):
                return bool(self.relay_controller.is_security_relays_on())
        except Exception:
            logger.exception("Security relay GPIO state read failed")
        return bool(self.state.relay_active)

    @staticmethod
    def _event_to_source_key(event: DetectionEvent) -> str | None:
        camera = str(event.camera_type)
        detection = str(event.detection_type)
        mapping = {
            ("internal", DetectionType.PERSON_DETECTED.value): "internal_person",
            ("internal", DetectionType.PERSON_CLEARED.value): "internal_person",
            ("internal", DetectionType.TAMPER_DETECTED.value): "internal_tamper",
            ("internal", DetectionType.TAMPER_CLEARED.value): "internal_tamper",
            ("external", DetectionType.TAMPER_DETECTED.value): "external_tamper",
            ("external", DetectionType.TAMPER_CLEARED.value): "external_tamper",
        }
        return mapping.get((camera, detection))

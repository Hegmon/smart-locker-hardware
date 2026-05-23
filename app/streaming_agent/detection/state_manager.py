from __future__ import annotations

import threading
import time
from dataclasses import replace

from app.streaming_agent.config.runtime import StreamingAgentRuntimeConfig
from app.streaming_agent.event_bus import DetectionEvent, DetectionType, EventBus
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager
from app.streaming_agent.relay.security_relay_manager import SecurityRelayManager


logger = LoggingManager.get_logger(__name__)
DEBUG_LOG_INTERVAL = 2.0


class DetectionStateManager:
    """Centralized detection lifecycle and event publisher.

    Detectors report their current debounced state here. This manager emits
    `*_DETECTED` and `*_CLEARED` transitions exactly once per state change.
    """

    def __init__(
        self,
        relay_controller,
        *,
        security_hold_seconds=None,
        detection_hold_seconds=None,
        tamper_hold_seconds=None,
        runtime_config: StreamingAgentRuntimeConfig | None = None,
        event_bus: EventBus | None = None,
        relay_manager: SecurityRelayManager | None = None,
    ):
        self.runtime_config = runtime_config or StreamingAgentRuntimeConfig.from_env()
        hold_seconds = self.runtime_config.relay.timeout_seconds
        if security_hold_seconds is not None:
            hold_seconds = max(0.0, float(security_hold_seconds))
        if detection_hold_seconds is not None:
            hold_seconds = max(0.0, float(detection_hold_seconds))
        if tamper_hold_seconds is not None:
            hold_seconds = max(hold_seconds, max(0.0, float(tamper_hold_seconds)))
        self.security_hold_seconds = hold_seconds
        self.event_bus = event_bus or EventBus()
        relay_config = replace(
            self.runtime_config.relay,
            timeout_seconds=self.security_hold_seconds,
        )
        self.relay_manager = relay_manager or SecurityRelayManager(
            relay_controller,
            config=relay_config,
        )
        self.event_bus.subscribe("*", self.relay_manager.handle_detection_event)
        self.camera_state = {
            "internal": self._new_camera_state(),
            "external": self._new_camera_state(),
        }
        self._lock = threading.RLock()
        self._last_debug_log_at = 0.0

    @staticmethod
    def _new_camera_state():
        return {
            "face_detected": False,
            "hand_detected": False,
            "person_detected": False,
            "motion_detected": False,
            "tamper_detected": False,
            "human_score": 0.0,
            "last_face_time": 0.0,
            "last_hand_time": 0.0,
            "last_person_time": 0.0,
            "last_motion_time": 0.0,
            "last_tamper_time": 0.0,
            "last_person_event_time": 0.0,
            "last_motion_event_time": 0.0,
            "last_tamper_event_time": 0.0,
        }

    def update_presence(
        self,
        camera_role,
        *,
        face_detected=False,
        hand_detected=False,
        person_detected=False,
        motion_detected=False,
        human_score=0.0,
        reason="",
    ):
        now = time.monotonic()
        with self._lock:
            state = self._state_for(camera_role)
            state["human_score"] = float(human_score or 0.0)
            self._update_signal_locked(state, camera_role, "face", bool(face_detected), now, reason)
            self._update_signal_locked(state, camera_role, "hand", bool(hand_detected), now, reason)
            self._update_signal_locked(state, camera_role, "person", bool(person_detected), now, reason, human_score=human_score)
            self._update_signal_locked(state, camera_role, "motion", bool(motion_detected), now, reason, human_score=human_score)
            self._maybe_log_snapshot_locked(now)

    def update_tamper(self, camera_role, *, tamper_detected=False, reason=""):
        now = time.monotonic()
        with self._lock:
            state = self._state_for(camera_role)
            self._update_signal_locked(state, camera_role, "tamper", bool(tamper_detected), now, reason)
            self._maybe_log_snapshot_locked(now)

    def clear_presence(self, camera_role="internal"):
        with self._lock:
            state = self._state_for(camera_role)
            for signal in ("face", "hand", "person", "motion"):
                self._update_signal_locked(state, camera_role, signal, False, time.monotonic(), "clear_presence")
            state["human_score"] = 0.0

    def clear_tamper(self, camera_role):
        with self._lock:
            state = self._state_for(camera_role)
            self._update_signal_locked(state, camera_role, "tamper", False, time.monotonic(), "clear_tamper")

    def check_timeouts(self):
        with self._lock:
            self._maybe_log_snapshot_locked(time.monotonic())

    def _update_signal_locked(self, state, camera_role, signal, active, now, reason, *, human_score=0.0):
        state_key = f"{signal}_detected"
        time_key = f"last_{signal}_time"
        event_time_key = f"last_{signal}_event_time"
        previous = bool(state.get(state_key))
        if previous == active:
            if active:
                state[time_key] = now
                if signal in {"person", "motion", "tamper"}:
                    debounce_seconds = self.runtime_config.relay.detection_debounce_seconds
                    last_event_time = float(state.get(event_time_key, 0.0))
                    if now - last_event_time >= debounce_seconds:
                        detection_type = self._transition_event_type(signal, True)
                        confidence = 1.0 if signal == "tamper" else float(human_score or 0.0)
                        self._publish_detection(camera_role, detection_type, confidence, now, f"{reason or signal}_refresh")
                        state[event_time_key] = now
            return

        state[state_key] = active
        state[time_key] = now if active else 0.0
        self._log_signal_change(camera_role, signal, active, reason)
        detection_type = self._transition_event_type(signal, active)
        if detection_type is None:
            return
        confidence = float(human_score or 0.0)
        if signal == "tamper":
            confidence = 1.0
        self._publish_detection(camera_role, detection_type, confidence, now, reason)
        state[event_time_key] = now if active else 0.0

    def _publish_detection(self, camera_role, detection_type, confidence, timestamp, reason):
        event = DetectionEvent(
            camera_type=str(camera_role or "internal"),
            detection_type=str(detection_type),
            confidence=float(confidence or 0.0),
            timestamp=timestamp,
            reason=str(reason or ""),
        )
        logger.info(
            "Detection transition event=%s camera=%s event_id=%s confidence=%.2f reason=%s",
            event.detection_type,
            event.camera_type,
            event.event_id,
            event.confidence,
            event.reason or "n/a",
        )
        self.event_bus.publish(event.event_name, event)

    def _state_for(self, camera_role):
        role = str(camera_role or "internal")
        if role not in self.camera_state:
            self.camera_state[role] = self._new_camera_state()
        return self.camera_state[role]

    def _maybe_log_snapshot_locked(self, now):
        if now - self._last_debug_log_at < DEBUG_LOG_INTERVAL:
            return
        parts = []
        for role, state in self.camera_state.items():
            parts.append(
                f"{role}:person={state['person_detected']} motion={state['motion_detected']} "
                f"face={state['face_detected']} hand={state['hand_detected']} tamper={state['tamper_detected']}"
            )
        logger.info("Detection snapshot %s", " | ".join(parts))
        self._last_debug_log_at = now

    @staticmethod
    def _transition_event_type(signal, active):
        mapping = {
            ("person", True): DetectionType.PERSON_DETECTED.value,
            ("person", False): DetectionType.PERSON_CLEARED.value,
            ("motion", True): DetectionType.MOTION_DETECTED.value,
            ("motion", False): DetectionType.MOTION_CLEARED.value,
            ("tamper", True): DetectionType.TAMPER_DETECTED.value,
            ("tamper", False): DetectionType.TAMPER_CLEARED.value,
        }
        return mapping.get((signal, active))

    @staticmethod
    def _log_signal_change(camera_role, signal, active, reason):
        if active:
            logger.info("%s state changed on %s camera: INACTIVE -> ACTIVE reason=%s", signal, camera_role, reason or signal)
        else:
            logger.info("%s state changed on %s camera: ACTIVE -> INACTIVE", signal, camera_role)

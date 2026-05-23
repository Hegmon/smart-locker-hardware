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
    """Thread-safe detection registry and centralized event publisher.

    Detectors report debounced observations here. This class keeps a current
    snapshot for each camera, logs transitions, and forwards supported
    detections into the single relay-event pipeline.
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
            if face_detected:
                state["last_face_time"] = now
            if hand_detected:
                state["last_hand_time"] = now
            if person_detected:
                state["last_person_time"] = now
                self._publish_detection(camera_role, DetectionType.PERSON_DETECTED.value, human_score, now, reason)
            if motion_detected:
                state["last_motion_time"] = now
                self._publish_detection(camera_role, DetectionType.MOTION_DETECTED.value, human_score, now, reason)
            state["human_score"] = max(float(human_score or 0.0), state.get("human_score", 0.0) * 0.8)
            self._apply_locked(now, reason=reason)

    def update_tamper(self, camera_role, *, tamper_detected=False, reason=""):
        now = time.monotonic()
        with self._lock:
            state = self._state_for(camera_role)
            if tamper_detected:
                state["last_tamper_time"] = now
                self._publish_detection(camera_role, DetectionType.TAMPER_DETECTED.value, 1.0, now, reason)
            self._apply_locked(now, reason=reason)

    def clear_presence(self, camera_role="internal"):
        with self._lock:
            state = self._state_for(camera_role)
            state["last_person_time"] = 0.0
            state["last_motion_time"] = 0.0
            state["last_face_time"] = 0.0
            state["last_hand_time"] = 0.0
            state["human_score"] = 0.0
            self._apply_locked(time.monotonic(), reason="clear_presence")

    def clear_tamper(self, camera_role):
        with self._lock:
            state = self._state_for(camera_role)
            state["last_tamper_time"] = 0.0
            self._apply_locked(time.monotonic(), reason="clear_tamper")

    def check_timeouts(self):
        with self._lock:
            self._apply_locked(time.monotonic(), reason="timeout_check")

    def _publish_detection(self, camera_role, detection_type, confidence, timestamp, reason):
        event = DetectionEvent(
            camera_type=str(camera_role or "internal"),
            detection_type=str(detection_type),
            confidence=float(confidence or 0.0),
            timestamp=timestamp,
            reason=str(reason or ""),
        )
        logger.info(
            "Detection event published event_id=%s camera=%s detection=%s confidence=%.2f reason=%s",
            event.event_id,
            event.camera_type,
            event.detection_type,
            event.confidence,
            event.reason or "n/a",
        )
        self.event_bus.publish(event.event_name, event)

    def _state_for(self, camera_role):
        role = str(camera_role or "internal")
        if role not in self.camera_state:
            self.camera_state[role] = self._new_camera_state()
        return self.camera_state[role]

    def _apply_locked(self, now, *, reason=""):
        for role, state in self.camera_state.items():
            previous_face = state["face_detected"]
            previous_hand = state["hand_detected"]
            previous_person = state["person_detected"]
            previous_motion = state["motion_detected"]
            previous_tamper = state["tamper_detected"]

            state["face_detected"] = self._within_hold(now, state["last_face_time"], self.security_hold_seconds)
            state["hand_detected"] = self._within_hold(now, state["last_hand_time"], self.security_hold_seconds)
            state["person_detected"] = self._within_hold(now, state["last_person_time"], self.security_hold_seconds)
            state["motion_detected"] = self._within_hold(now, state["last_motion_time"], self.security_hold_seconds)
            state["tamper_detected"] = self._within_hold(now, state["last_tamper_time"], self.security_hold_seconds)

            if not state["face_detected"]:
                state["last_face_time"] = 0.0
            if not state["hand_detected"]:
                state["last_hand_time"] = 0.0
            if not state["person_detected"]:
                state["last_person_time"] = 0.0
            if not state["motion_detected"]:
                state["last_motion_time"] = 0.0
            if not state["tamper_detected"]:
                state["last_tamper_time"] = 0.0

            if state["face_detected"] != previous_face:
                self._log_signal_change(role, "face", state["face_detected"], reason)
            if state["hand_detected"] != previous_hand:
                self._log_signal_change(role, "hand", state["hand_detected"], reason)
            if state["person_detected"] != previous_person:
                self._log_signal_change(role, "person", state["person_detected"], reason)
            if state["motion_detected"] != previous_motion:
                self._log_signal_change(role, "motion", state["motion_detected"], reason)
            if state["tamper_detected"] != previous_tamper:
                self._log_signal_change(role, "tamper", state["tamper_detected"], reason)

        if now - self._last_debug_log_at >= DEBUG_LOG_INTERVAL:
            parts = []
            for role, state in self.camera_state.items():
                parts.append(
                    f"{role}:person={state['person_detected']} motion={state['motion_detected']} "
                    f"face={state['face_detected']} hand={state['hand_detected']} tamper={state['tamper_detected']}"
                )
            logger.info("Detection snapshot %s", " | ".join(parts))
            self._last_debug_log_at = now

    @staticmethod
    def _within_hold(now, last_seen_at, hold_seconds):
        try:
            return bool(last_seen_at and (now - last_seen_at) < float(hold_seconds))
        except Exception:
            return False

    @staticmethod
    def _log_signal_change(camera_role, signal, active, reason):
        if active:
            logger.info("%s detection active on %s camera: %s", signal, camera_role, reason or signal)
        else:
            logger.info("%s detection cleared on %s camera", signal, camera_role)

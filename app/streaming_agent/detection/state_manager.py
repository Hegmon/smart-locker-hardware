import os
import threading
import time

from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)

DEFAULT_SECURITY_HOLD_SECONDS = 8.0


def _env_float(name, default, minimum=None):
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    return value


class DetectionStateManager:
    """Thread-safe detection state and relay decision owner.

    Detectors report debounced observations here. This class owns hold timers,
    derived relay state, and edge-triggered logging.
    """

    def __init__(self, relay_controller, *, security_hold_seconds=None, detection_hold_seconds=None, tamper_hold_seconds=None):
        self.relay_controller = relay_controller
        default_hold = _env_float("SECURITY_HOLD_SECONDS", DEFAULT_SECURITY_HOLD_SECONDS, minimum=0.0)
        self.security_hold_seconds = (
            default_hold
            if security_hold_seconds is None
            else max(0.0, float(security_hold_seconds))
        )
        if detection_hold_seconds is not None:
            self.security_hold_seconds = max(0.0, float(detection_hold_seconds))
        if tamper_hold_seconds is not None:
            self.security_hold_seconds = max(self.security_hold_seconds, max(0.0, float(tamper_hold_seconds)))
        self.camera_state = {
            "internal": self._new_camera_state(),
            "external": self._new_camera_state(),
        }
        self._lock = threading.RLock()
        self._security_event_active = False
        self._expiry_thread = None

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
            if motion_detected:
                state["last_motion_time"] = now
            state["human_score"] = max(float(human_score or 0.0), state.get("human_score", 0.0) * 0.8)
            self._apply_locked(now, reason=reason)

    def update_tamper(self, camera_role, *, tamper_detected=False, reason=""):
        now = time.monotonic()
        with self._lock:
            state = self._state_for(camera_role)
            if tamper_detected:
                state["last_tamper_time"] = now
            self._apply_locked(now, reason=reason)

    def clear_presence(self, camera_role="internal"):
        with self._lock:
            state = self._state_for(camera_role)
            state["last_person_time"] = 0.0
            state["last_motion_time"] = 0.0
            state["last_face_time"] = 0.0
            state["last_hand_time"] = 0.0
            state["human_score"] = 0.0
            self._apply_locked(time.monotonic(), force=True)

    def clear_tamper(self, camera_role):
        with self._lock:
            state = self._state_for(camera_role)
            state["last_tamper_time"] = 0.0
            self._apply_locked(time.monotonic(), force=True)

    def check_timeouts(self):
        with self._lock:
            self._apply_locked(time.monotonic())

    def _state_for(self, camera_role):
        role = str(camera_role or "internal")
        if role not in self.camera_state:
            self.camera_state[role] = self._new_camera_state()
        return self.camera_state[role]

    def _apply_locked(self, now, *, reason="", force=False):
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

        security_event_active = any(
            bool(
                state.get("face_detected")
                or state.get("hand_detected")
                or state.get("person_detected")
                or state.get("motion_detected")
                or state.get("tamper_detected")
            )
            for state in self.camera_state.values()
        )

        if force or security_event_active != self._security_event_active:
            self._security_event_active = security_event_active
            if security_event_active:
                logger.warning("Unified security event ACTIVE")
            else:
                logger.info("Unified security event CLEARED")
            logger.info("Relays synchronized %s", "ON" if security_event_active else "OFF")
            self.relay_controller.set_security_relays(security_event_active)

        if security_event_active:
            self._ensure_expiry_thread_locked()

    @staticmethod
    def _within_hold(now, last_seen_at, hold_seconds):
        return bool(last_seen_at and now - last_seen_at <= hold_seconds)

    def _ensure_expiry_thread_locked(self):
        if self._expiry_thread and self._expiry_thread.is_alive():
            return
        self._expiry_thread = threading.Thread(
            target=self._expiry_worker,
            daemon=True,
            name="detection-state-expiry",
        )
        self._expiry_thread.start()

    def _expiry_worker(self):
        while True:
            time.sleep(0.1)
            with self._lock:
                self._apply_locked(time.monotonic())
                if not self._security_event_active:
                    return

    @staticmethod
    def _log_signal_change(camera_role, signal, active, reason):
        if active:
            logger.info("%s detection active on %s camera: %s", signal, camera_role, reason or signal)
        else:
            logger.info("%s detection cleared on %s camera after hold timeout", signal, camera_role)

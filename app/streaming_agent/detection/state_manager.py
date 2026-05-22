import os
import threading
import time

from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)

DEFAULT_DETECTION_HOLD_SECONDS = 5.0
DEFAULT_TAMPER_HOLD_SECONDS = 5.0


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

    def __init__(self, relay_controller, *, detection_hold_seconds=None, tamper_hold_seconds=None):
        self.relay_controller = relay_controller
        self.detection_hold_seconds = (
            _env_float("DETECTION_HOLD_SECONDS", DEFAULT_DETECTION_HOLD_SECONDS, minimum=0.0)
            if detection_hold_seconds is None
            else max(0.0, float(detection_hold_seconds))
        )
        self.tamper_hold_seconds = (
            _env_float("TAMPER_HOLD_SECONDS", DEFAULT_TAMPER_HOLD_SECONDS, minimum=0.0)
            if tamper_hold_seconds is None
            else max(0.0, float(tamper_hold_seconds))
        )
        self.camera_state = {
            "internal": self._new_camera_state(),
            "external": self._new_camera_state(),
        }
        self._lock = threading.RLock()
        self._relay1_active = False
        self._relay4_active = False
        self._expiry_thread = None

    @staticmethod
    def _new_camera_state():
        return {
            "person_detected": False,
            "motion_detected": False,
            "tamper_detected": False,
            "last_person_time": 0.0,
            "last_motion_time": 0.0,
            "last_tamper_time": 0.0,
        }

    def update_presence(self, camera_role, *, person_detected=False, motion_detected=False, reason=""):
        now = time.monotonic()
        with self._lock:
            state = self._state_for(camera_role)
            if person_detected:
                state["last_person_time"] = now
            if motion_detected:
                state["last_motion_time"] = now
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
            previous_person = state["person_detected"]
            previous_motion = state["motion_detected"]
            previous_tamper = state["tamper_detected"]
            state["person_detected"] = self._within_hold(now, state["last_person_time"], self.detection_hold_seconds)
            state["motion_detected"] = self._within_hold(now, state["last_motion_time"], self.detection_hold_seconds)
            state["tamper_detected"] = self._within_hold(now, state["last_tamper_time"], self.tamper_hold_seconds)
            if state["person_detected"] != previous_person:
                self._log_signal_change(role, "person", state["person_detected"], reason)
            if state["motion_detected"] != previous_motion:
                self._log_signal_change(role, "motion", state["motion_detected"], reason)
            if state["tamper_detected"] != previous_tamper:
                self._log_signal_change(role, "tamper", state["tamper_detected"], reason)

        internal = self.camera_state.get("internal", {})
        relay1_active = bool(internal.get("person_detected") or internal.get("motion_detected"))
        relay4_active = any(bool(state.get("tamper_detected")) for state in self.camera_state.values())

        if force or relay1_active != self._relay1_active:
            self._relay1_active = relay1_active
            logger.info("Relay 1 changed to %s", "ON" if relay1_active else "OFF")
        if relay1_active:
            self.relay_controller.set_person_visible(True)
        elif force or self._relay1_active == relay1_active:
            self.relay_controller.set_person_visible(False)

        if force or relay4_active != self._relay4_active:
            self._relay4_active = relay4_active
            logger.info("Relay 4 changed to %s", "ON" if relay4_active else "OFF")
        if relay4_active:
            self.relay_controller.set_tamper_active("any", True)
        elif force or self._relay4_active == relay4_active:
            self.relay_controller.set_tamper_active("any", False)

        if relay1_active or relay4_active:
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
                if not self._relay1_active and not self._relay4_active:
                    return

    @staticmethod
    def _log_signal_change(camera_role, signal, active, reason):
        if active:
            logger.info("%s detection active on %s camera: %s", signal, camera_role, reason or signal)
        else:
            logger.info("%s detection cleared on %s camera after hold timeout", signal, camera_role)

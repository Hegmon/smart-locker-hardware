import os
import threading
import time

from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)

DEFAULT_SECURITY_HOLD_SECONDS = 8.0
DEFAULT_MAX_STALE_SECONDS = 15.0
DEBUG_LOG_INTERVAL = 2.0
RELAY_REFRESH_INTERVAL = 1.0


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
        self._last_debug_log_at = 0.0
        self._last_relay_refresh_at = 0.0
        self._max_stale_seconds = DEFAULT_MAX_STALE_SECONDS
        # start verifier thread that ensures relays match computed state
        self._verifier_thread = threading.Thread(
            target=self._verification_worker,
            daemon=True,
            name="detection-state-verifier",
        )
        self._verifier_thread.start()

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
            # compute strict within-hold (< not <=) activity
            face_active = self._within_hold(now, state["last_face_time"], self.security_hold_seconds)
            hand_active = self._within_hold(now, state["last_hand_time"], self.security_hold_seconds)
            person_active = self._within_hold(now, state["last_person_time"], self.security_hold_seconds)
            motion_active = self._within_hold(now, state["last_motion_time"], self.security_hold_seconds)
            tamper_active = self._within_hold(now, state["last_tamper_time"], self.security_hold_seconds)

            state["face_detected"] = bool(face_active)
            state["hand_detected"] = bool(hand_active)
            state["person_detected"] = bool(person_active)
            state["motion_detected"] = bool(motion_active)
            state["tamper_detected"] = bool(tamper_active)

            # when hold expires clear timestamps to avoid stale data
            if not face_active and state.get("last_face_time"):
                state["last_face_time"] = 0.0
            if not hand_active and state.get("last_hand_time"):
                state["last_hand_time"] = 0.0
            if not person_active and state.get("last_person_time"):
                state["last_person_time"] = 0.0
            if not motion_active and state.get("last_motion_time"):
                state["last_motion_time"] = 0.0
            if not tamper_active and state.get("last_tamper_time"):
                state["last_tamper_time"] = 0.0

            # stale-state watchdog: forcibly clear very old timestamps
            stale_threshold = self._max_stale_seconds
            for key, last_key in (
                ("face_detected", "last_face_time"),
                ("hand_detected", "last_hand_time"),
                ("person_detected", "last_person_time"),
                ("motion_detected", "last_motion_time"),
                ("tamper_detected", "last_tamper_time"),
            ):
                last_val = state.get(last_key, 0.0)
                if last_val and now - last_val > stale_threshold:
                    logger.warning("Clearing stale detector state for %s on %s camera", key, role)
                    state[last_key] = 0.0
                    state[key] = False
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

        # periodic debug logging for easier diagnosis of stuck relays
        try:
            if now - self._last_debug_log_at >= DEBUG_LOG_INTERVAL:
                parts = []
                for role, state in self.camera_state.items():
                    parts.append(
                        f"{role}:person={state.get('person_detected')} motion={state.get('motion_detected')} face={state.get('face_detected')} hand={state.get('hand_detected')} tamper={state.get('tamper_detected')}"
                    )
                logger.info(
                    "Security state: %s security=%s",
                    " | ".join(parts),
                    bool(security_event_active),
                )
                # flat summary matching required debug format
                person_active = any(bool(s.get("person_detected")) for s in self.camera_state.values())
                motion_active = any(bool(s.get("motion_detected")) for s in self.camera_state.values())
                face_active = any(bool(s.get("face_detected")) for s in self.camera_state.values())
                hand_active = any(bool(s.get("hand_detected")) for s in self.camera_state.values())
                tamper_active = any(bool(s.get("tamper_detected")) for s in self.camera_state.values())
                logger.info(
                    "Security state: person=%s motion=%s face=%s hand=%s tamper=%s security=%s",
                    person_active,
                    motion_active,
                    face_active,
                    hand_active,
                    tamper_active,
                    bool(security_event_active),
                )
                self._last_debug_log_at = now
        except Exception:
            logger.exception("Failed while logging security debug state")

        # synchronize relays; refresh TTL while active to avoid relay controller expiry
        try:
            if force or security_event_active != self._security_event_active:
                self._security_event_active = security_event_active
                if security_event_active:
                    logger.warning("Unified security event ACTIVE")
                else:
                    logger.info("Unified security event CLEARED")
                logger.info("Relays synchronized %s", "ON" if security_event_active else "OFF")
                self.relay_controller.set_security_relays(security_event_active)

            # refresh TTL periodically while active so relay controller doesn't expire the source
            if security_event_active:
                if now - self._last_relay_refresh_at >= RELAY_REFRESH_INTERVAL:
                    self.relay_controller.set_security_relays(True)
                    self._last_relay_refresh_at = now
        except Exception:
            logger.exception("Failed to synchronize relays with detection state manager")

        if security_event_active:
            self._ensure_expiry_thread_locked()

    def _verification_worker(self):
        # Runs continuously to verify relays reflect computed security state
        while True:
            try:
                with self._lock:
                    # re-evaluate timeouts and states (run immediately on start)
                    self._apply_locked(time.monotonic())
                    if not self._security_event_active:
                        # if computed state says no security event, relays must be OFF
                        try:
                            if hasattr(self.relay_controller, "is_security_relays_on") and self.relay_controller.is_security_relays_on():
                                logger.error("Relay mismatch: computed security_clear but hardware reports ON; forcing OFF")
                                # attempt graceful clear first
                                try:
                                    self.relay_controller.set_security_relays(False)
                                except Exception:
                                    logger.exception("Failed to clear relays gracefully; forcing hardware OFF")
                                # force hardware OFF if still mismatched
                                try:
                                    if hasattr(self.relay_controller, "force_security_relays_off"):
                                        self.relay_controller.force_security_relays_off()
                                except Exception:
                                    logger.exception("Failed to force relays OFF")
                        except Exception:
                            logger.exception("Verification check failed")
                time.sleep(1.0)
            except Exception:
                logger.exception("Security verification thread failed")

    @staticmethod
    def _within_hold(now, last_seen_at, hold_seconds):
        # strict '<' semantics: expiration occurs once age >= hold_seconds
        try:
            return bool(last_seen_at and (now - last_seen_at) < float(hold_seconds))
        except Exception:
            return False

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

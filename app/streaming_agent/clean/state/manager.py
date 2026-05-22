"""Centralized security state manager.

Small, focused, thread-safe manager that collects detector timestamps and
produces the single `security_active` decision that drives the relays.

Detectors only call `report_detection(camera_role, detector_name, confirmed=True)`
when they have a confirmed detection (score checks belong in detectors).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict

logger = logging.getLogger(__name__)


@dataclass
class CameraState:
    person: bool = False
    motion: bool = False
    face: bool = False
    hand: bool = False
    tamper: bool = False
    last_person: float = 0.0
    last_motion: float = 0.0
    last_face: float = 0.0
    last_hand: float = 0.0
    last_tamper: float = 0.0


@dataclass
class SecurityStateManager:
    relay_controller: object
    security_hold_seconds: float = 8.0
    stale_seconds: float = 15.0
    debug_interval: float = 2.0
    verify_interval: float = 1.0
    loop_interval: float = 0.1

    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    cameras: Dict[str, CameraState] = field(default_factory=lambda: {"internal": CameraState(), "external": CameraState()}, init=False)
    _security_active: bool = field(default=False, init=False)
    _stop: bool = field(default=False, init=False)

    def __post_init__(self):
        self._last_debug = 0.0
        self._last_verify = 0.0
        self._thread = threading.Thread(target=self._loop, daemon=True, name="security-state-loop")
        self._thread.start()

    def report_detection(self, camera_role: str, detector: str, *, confirmed: bool = True, score: float | None = None, threshold: float | None = None) -> None:
        """Called by detectors when they see a confirmed event.

        Only confirmed detections update timestamps. Weak/low-score detections
        should not call this method or should pass `confirmed=False`.
        """
        if not confirmed:
            return
        now = time.monotonic()
        with self._lock:
            state = self._state_for(camera_role)
            if detector == "person":
                state.last_person = now
            elif detector == "motion":
                state.last_motion = now
            elif detector == "face":
                state.last_face = now
            elif detector == "hand":
                state.last_hand = now
            elif detector == "tamper":
                state.last_tamper = now

    def _state_for(self, camera_role: str) -> CameraState:
        role = str(camera_role or "internal")
        if role not in self.cameras:
            self.cameras[role] = CameraState()
        return self.cameras[role]

    def stop(self) -> None:
        self._stop = True
        try:
            self._thread.join(timeout=1.0)
        except Exception:
            pass

    @staticmethod
    def _within(now: float, last: float, hold: float) -> bool:
        try:
            return bool(last and (now - last) < float(hold))
        except Exception:
            return False

    def _compute_and_apply(self) -> None:
        now = time.monotonic()
        internal_security = False
        external_security = False
        with self._lock:
            # compute per-camera and clear expired timestamps
            for role, state in self.cameras.items():
                person_active = self._within(now, state.last_person, self.security_hold_seconds)
                motion_active = self._within(now, state.last_motion, self.security_hold_seconds)
                face_active = self._within(now, state.last_face, self.security_hold_seconds)
                hand_active = self._within(now, state.last_hand, self.security_hold_seconds)
                tamper_active = self._within(now, state.last_tamper, self.security_hold_seconds)

                # clear timestamps when expired to avoid stale data
                if not person_active and state.last_person:
                    state.last_person = 0.0
                if not motion_active and state.last_motion:
                    state.last_motion = 0.0
                if not face_active and state.last_face:
                    state.last_face = 0.0
                if not hand_active and state.last_hand:
                    state.last_hand = 0.0
                if not tamper_active and state.last_tamper:
                    state.last_tamper = 0.0

                # assign boolean flags
                state.person = bool(person_active)
                state.motion = bool(motion_active)
                state.face = bool(face_active)
                state.hand = bool(hand_active)
                state.tamper = bool(tamper_active)

                # stale watchdog
                for last_attr in ("last_person", "last_motion", "last_face", "last_hand", "last_tamper"):
                    last_val = getattr(state, last_attr)
                    if last_val and (now - last_val) > self.stale_seconds:
                        logger.warning("Clearing stale %s on %s", last_attr, role)
                        setattr(state, last_attr, 0.0)
                        setattr(state, last_attr.replace("last_", ""), False)

            internal = self.cameras.get("internal")
            external = self.cameras.get("external")
            if internal:
                internal_security = bool(internal.person or internal.motion or internal.hand or internal.face or internal.tamper)
            if external:
                external_security = bool(external.tamper)

            security_active = bool(internal_security or external_security)

            # change detection
            if security_active != self._security_active:
                self._security_active = security_active
                if security_active:
                    logger.warning("Unified security event ACTIVE")
                else:
                    logger.info("Unified security event CLEARED")
                try:
                    self.relay_controller.set_security_relays(security_active)
                except Exception:
                    logger.exception("Failed to update relays from state manager")

        # debug log periodically (outside lock)
        if now - self._last_debug >= self.debug_interval:
            self._last_debug = now
            try:
                parts = []
                with self._lock:
                    for role, st in self.cameras.items():
                        parts.append(f"{role}: person={st.person} motion={st.motion} face={st.face} hand={st.hand} tamper={st.tamper}")
                logger.info("Security state: %s security=%s", " | ".join(parts), bool(self._security_active))
            except Exception:
                logger.exception("Failed debug logging for security state")

        # verification: ensure hardware matches computed OFF
        if not self._security_active and (now - self._last_verify) >= self.verify_interval:
            self._last_verify = now
            try:
                if hasattr(self.relay_controller, "is_security_relays_on") and self.relay_controller.is_security_relays_on():
                    logger.error("Relay mismatch detected: hardware reports ON while computed state is CLEAR; forcing OFF")
                    try:
                        if hasattr(self.relay_controller, "force_security_relays_off"):
                            self.relay_controller.force_security_relays_off()
                    except Exception:
                        logger.exception("Failed to force relays off during verification")
            except Exception:
                logger.exception("Verification check failed")

    def _loop(self) -> None:
        while not self._stop:
            try:
                self._compute_and_apply()
            except Exception:
                logger.exception("SecurityStateManager loop failed")
            time.sleep(self.loop_interval)

    def get_camera_state(self, camera_role: str) -> CameraState:
        with self._lock:
            return self._state_for(camera_role)

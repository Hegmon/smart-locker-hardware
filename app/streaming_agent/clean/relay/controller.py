"""Simple centralized relay controller.

Single authoritative API for controlling security relays (Relay 1 & Relay 4).
Detectors MUST NOT touch GPIO directly; they only inform the state manager.

This controller is small and testable. It accepts a GPIO-like backend for
real hardware; tests use the fake backend below.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class _FakeGPIO:
    """Minimal fake GPIO backend used for testing and dry-run."""
    HIGH = 1
    LOW = 0

    def __init__(self):
        self._pins = {}

    def setmode(self, mode):
        return None

    def setwarnings(self, enabled):
        return None

    def setup(self, pin, direction, initial=None):
        self._pins[int(pin)] = initial

    def output(self, pin, state):
        self._pins[int(pin)] = int(state)

    def input(self, pin):
        return self._pins.get(int(pin), self.HIGH)

    def cleanup(self, pins=None):
        for p in tuple(self._pins.keys()):
            if pins is None or p in pins:
                self._pins.pop(p, None)


class CentralRelayController:
    """Thread-safe simple relay controller.

    Methods:
    - `set_security_relays(active: bool)` — authoritative call to set both relays.
    - `is_security_relays_on()` — read-back (prefers GPIO read if backend supports it).
    - `force_security_relays_off()` — immediate hardware-level off and internal sync.
    """

    def __init__(self, *, relay1_pin: int = 21, relay4_pin: int = 12, gpio_backend: Optional[object] = None, active_low: bool = True):
        self.relay1_pin = int(relay1_pin)
        self.relay4_pin = int(relay4_pin)
        self.active_low = bool(active_low)
        self._lock = threading.RLock()
        self._state = False
        self._gpio = gpio_backend or _FakeGPIO()
        self._enabled = False

    def start(self):
        with self._lock:
            if self._enabled:
                return
            try:
                # Configure pins (backend may be fake)
                self._gpio.setmode(getattr(self._gpio, "BCM", None))
            except Exception:
                pass
            try:
                self._gpio.setwarnings(False)
            except Exception:
                pass
            try:
                self._gpio.setup(self.relay1_pin, getattr(self._gpio, "OUT", None), initial=self._inactive_level())
                self._gpio.setup(self.relay4_pin, getattr(self._gpio, "OUT", None), initial=self._inactive_level())
            except Exception:
                # backend may not support setup; ignore and rely on output
                pass
            # Ensure relays are off on start
            self._write_levels(False)
            self._enabled = True
            logger.info("CentralRelayController initialized; active_low=%s", self.active_low)

    def _active_level(self):
        return self._gpio.LOW if self.active_low and hasattr(self._gpio, "LOW") else 0

    def _inactive_level(self):
        return self._gpio.HIGH if self.active_low and hasattr(self._gpio, "HIGH") else 1

    def _write_levels(self, active: bool):
        level = self._active_level() if active else self._inactive_level()
        try:
            self._gpio.output(self.relay1_pin, level)
            self._gpio.output(self.relay4_pin, level)
        except Exception:
            logger.info("Relay dry-run: set security relays %s", "ON" if active else "OFF")

    def set_security_relays(self, active: bool) -> None:
        """Authoritative API — sets both relays together."""
        with self._lock:
            active = bool(active)
            if self._state == active:
                return
            self._state = active
            self._write_levels(active)
            logger.info("Relays synchronized -> %s", "ON" if active else "OFF")

    def is_security_relays_on(self) -> bool:
        with self._lock:
            # Prefer reading actual GPIO when available
            try:
                if hasattr(self._gpio, "input"):
                    r1 = self._gpio.input(self.relay1_pin)
                    r4 = self._gpio.input(self.relay4_pin)
                    # interpret according to active_low
                    if self.active_low and hasattr(self._gpio, "LOW") and hasattr(self._gpio, "HIGH"):
                        return (r1 == self._gpio.LOW) or (r4 == self._gpio.LOW)
                    return (r1 == self._gpio.HIGH) or (r4 == self._gpio.HIGH)
            except Exception:
                logger.debug("GPIO read-back unavailable; falling back to internal state")
            return bool(self._state)

    def force_security_relays_off(self) -> None:
        """Force both relays OFF at hardware level and sync internal state."""
        with self._lock:
            try:
                # Ensure backend initialized
                if not self._enabled:
                    try:
                        self.start()
                    except Exception:
                        logger.exception("Failed to init GPIO backend while forcing relays off")
                # write inactive level regardless
                level = self._inactive_level()
                try:
                    self._gpio.output(self.relay1_pin, level)
                    self._gpio.output(self.relay4_pin, level)
                except Exception:
                    logger.exception("Failed to force-relay GPIO writes; dry-run logging only")
            finally:
                self._state = False
                logger.warning("Relays forcefully set -> OFF")

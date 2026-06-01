from __future__ import annotations

"""Generic relay channel helper for inspection tests."""

import threading
import time
from dataclasses import dataclass
from typing import Callable

from app.inspection_agent.hardware.gpio_mapping import RELAY_CHANNELS
from app.utils.logger import get_logger


logger = get_logger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    import os

    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RelayChannelDescriptor:
    """Describes a single relay channel exposed to inspection tests."""

    name: str
    gpio_pin: int
    on: Callable[[], None]
    off: Callable[[], None]


class RelayController:
    """Safe relay helper that can pulse any mapped channel."""

    def __init__(self) -> None:
        self.active_low = _env_bool("RELAY_ACTIVE_LOW", True)
        self._gpio = None
        self._started = False
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            gpio, source = self._load_gpio_backend()
            if gpio is None:
                return
            self._gpio = gpio
            self._configure_gpio()
            self._started = True
            logger.info(
                "Inspection relay controller initialized with backend=%s active_low=%s",
                source,
                self.active_low,
            )

    def cleanup(self) -> None:
        with self._lock:
            if self._gpio is None:
                self._started = False
                return
            try:
                cleanup = getattr(self._gpio, "cleanup", None)
                if callable(cleanup):
                    cleanup()
            except Exception:
                logger.exception("Relay cleanup failed")
            finally:
                self._gpio = None
                self._started = False

    def pulse(self, channel_name: str, duration_seconds: float = 2.0) -> None:
        """Pulse a relay channel and always restore the safe OFF state."""

        descriptor = self._resolve(channel_name)
        self.start()
        descriptor.on()
        try:
            time.sleep(max(0.1, float(duration_seconds)))
        finally:
            descriptor.off()

    def set_channel(self, channel_name: str, active: bool) -> None:
        descriptor = self._resolve(channel_name)
        self.start()
        if active:
            descriptor.on()
        else:
            descriptor.off()

    def available_channels(self) -> tuple[str, ...]:
        return tuple(RELAY_CHANNELS.keys())

    def _resolve(self, channel_name: str) -> RelayChannelDescriptor:
        normalized = str(channel_name or "").strip().lower()
        if normalized == "red_led":
            return RelayChannelDescriptor("red_led", RELAY_CHANNELS["red_led"], self._red_led_on, self._red_led_off)
        if normalized == "green_led":
            return RelayChannelDescriptor("green_led", RELAY_CHANNELS["green_led"], self._green_led_on, self._green_led_off)
        if normalized == "solenoid":
            return RelayChannelDescriptor("solenoid", RELAY_CHANNELS["solenoid"], self._solenoid_on, self._solenoid_off)
        if normalized == "buzzer":
            return RelayChannelDescriptor("buzzer", RELAY_CHANNELS["buzzer"], self._buzzer_on, self._buzzer_off)
        raise ValueError(f"Unsupported relay channel: {channel_name}")

    def _load_gpio_backend(self):
        try:
            import RPi.GPIO as GPIO

            return GPIO, "RPi.GPIO"
        except Exception as rpi_exc:
            try:
                return _LgpioCompat(), "lgpio"
            except Exception as lgpio_exc:
                logger.warning("GPIO unavailable; relay actions disabled: RPi.GPIO=%s lgpio=%s", rpi_exc, lgpio_exc)
                return None, "unavailable"

    def _configure_gpio(self) -> None:
        gpio = self._gpio
        if gpio is None:
            return
        gpio.setwarnings(False)
        gpio.setmode(gpio.BCM)
        for pin in RELAY_CHANNELS.values():
            gpio.setup(pin, gpio.OUT, initial=gpio.HIGH if self.active_low else gpio.LOW)

    def _write(self, pin: int, active: bool) -> None:
        gpio = self._gpio
        if gpio is None:
            raise RuntimeError("GPIO backend not initialized")
        gpio.output(pin, gpio.LOW if (active and self.active_low) else gpio.HIGH if active else gpio.HIGH if self.active_low else gpio.LOW)

    def _read(self, pin: int) -> bool:
        gpio = self._gpio
        if gpio is None:
            return False
        try:
            raw = gpio.input(pin)
        except Exception:
            return False
        if self.active_low:
            return raw == gpio.LOW
        return raw == gpio.HIGH

    def _red_led_on(self) -> None:
        self._write(RELAY_CHANNELS["red_led"], True)

    def _red_led_off(self) -> None:
        self._write(RELAY_CHANNELS["red_led"], False)

    def _green_led_on(self) -> None:
        self._write(RELAY_CHANNELS["green_led"], True)

    def _green_led_off(self) -> None:
        self._write(RELAY_CHANNELS["green_led"], False)

    def _solenoid_on(self) -> None:
        self._write(RELAY_CHANNELS["solenoid"], True)

    def _solenoid_off(self) -> None:
        self._write(RELAY_CHANNELS["solenoid"], False)

    def _buzzer_on(self) -> None:
        self._write(RELAY_CHANNELS["buzzer"], True)

    def _buzzer_off(self) -> None:
        self._write(RELAY_CHANNELS["buzzer"], False)


class _LgpioCompat:
    """Small subset of lgpio used as a fallback when RPi.GPIO is unavailable."""

    BCM = "BCM"
    OUT = "OUT"
    HIGH = 1
    LOW = 0

    def __init__(self) -> None:
        import lgpio

        self._lgpio = lgpio
        self._chip = lgpio.gpiochip_open(0)

    def setwarnings(self, _enabled: bool) -> None:
        return None

    def setmode(self, _mode: str) -> None:
        return None

    def setup(self, pin: int, _direction: str, initial: int | None = None) -> None:
        level = self.HIGH if initial in {None, self.HIGH} else self.LOW
        self._lgpio.gpio_claim_output(self._chip, int(pin), level)

    def output(self, pin: int, state: int) -> None:
        self._lgpio.gpio_write(self._chip, int(pin), int(state))

    def input(self, pin: int) -> int:
        return int(self._lgpio.gpio_read(self._chip, int(pin)))

    def cleanup(self) -> None:
        try:
            self._lgpio.gpiochip_close(self._chip)
        except Exception:
            logger.debug("lgpio cleanup failed", exc_info=True)

from __future__ import annotations

"""Relay adapter for inspection tests."""

import time
from dataclasses import dataclass
from typing import Callable

from app.streaming_agent.gpio.relay_controller import RelayController as StreamingRelayController
from app.utils.logger import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True)
class RelayChannelDescriptor:
    """Describes a single relay channel exposed to inspection tests."""

    name: str
    gpio_pin: int
    on: Callable[[], None]
    off: Callable[[], None]


class RelayController:
    """Inspection relay helper that reuses the existing streaming relay logic."""

    def __init__(self) -> None:
        self._controller = StreamingRelayController()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._controller.start()
        self._started = True

    def cleanup(self) -> None:
        try:
            self._controller.cleanup()
        except Exception:
            logger.exception("Relay cleanup failed")
        finally:
            self._started = False

    def pulse(self, channel_name: str, duration_seconds: float = 2.0) -> None:
        """Pulse a relay channel and always restore the safe OFF state."""

        descriptor = self._resolve(channel_name)
        self.start()
        logger.info(
            "Pulsing inspection relay channel=%s gpio=%s duration_seconds=%s",
            descriptor.name,
            descriptor.gpio_pin,
            duration_seconds,
        )
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
        return ("red_led", "green_led", "solenoid", "buzzer")

    def _resolve(self, channel_name: str) -> RelayChannelDescriptor:
        normalized = str(channel_name or "").strip().lower()
        if normalized == "red_led":
            return RelayChannelDescriptor("red_led", 21, self._controller.red_led_on, self._controller.red_led_off)
        if normalized == "green_led":
            return RelayChannelDescriptor("green_led", 20, self._controller.green_led_on, self._controller.green_led_off)
        if normalized == "solenoid":
            return RelayChannelDescriptor("solenoid", 16, self._controller.unlock_locker, self._controller.lock_locker)
        if normalized == "buzzer":
            return RelayChannelDescriptor("buzzer", 12, self._controller.buzzer_on, self._controller.buzzer_off)
        raise ValueError(f"Unsupported relay channel: {channel_name}")

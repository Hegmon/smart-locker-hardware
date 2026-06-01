from __future__ import annotations

"""BCM GPIO mapping for the inspection agent."""

RED_LED_GPIO = 21
GREEN_LED_GPIO = 20
SOLENOID_GPIO = 16
BUZZER_GPIO = 12

RELAY_CHANNELS: dict[str, int] = {
    "red_led": RED_LED_GPIO,
    "green_led": GREEN_LED_GPIO,
    "solenoid": SOLENOID_GPIO,
    "buzzer": BUZZER_GPIO,
}

RELAY_CHANNEL_ORDER = ("red_led", "green_led", "solenoid", "buzzer")

#!/usr/bin/env python3
"""Interactive relay channel diagnostic for Raspberry Pi BCM GPIO wiring."""

import argparse
import sys
import time
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app.streaming_agent.gpio.relay_controller import RelayController  # noqa: E402


CHANNELS = (
    ("IN1", "Red LED", "red_led_on", "red_led_off", 21),
    ("IN2", "Green LED", "green_led_on", "green_led_off", 20),
    ("IN3", "Locker", "unlock_locker", "lock_locker", 16),
    ("IN4", "Buzzer", "buzzer_on", "buzzer_off", 12),
)


def main():
    parser = argparse.ArgumentParser(description="Pulse each smart-locker relay channel.")
    parser.add_argument("--seconds", type=float, default=2.0, help="Seconds to keep each relay ON")
    parser.add_argument("--pause", type=float, default=1.0, help="Seconds to pause between relays")
    args = parser.parse_args()

    relay = RelayController()
    relay.start()
    print("")
    print("Relay diagnostic using BCM GPIO mapping:")
    print("  IN1 -> GPIO21 -> Red LED")
    print("  IN2 -> GPIO20 -> Green LED")
    print("  IN3 -> GPIO16 -> Locker")
    print("  IN4 -> GPIO12 -> Buzzer")
    print("")
    print("Watch which physical device turns ON for each channel.")
    print("")

    try:
        for relay_input, expected_device, on_method, off_method, gpio_pin in CHANNELS:
            print(f"{relay_input} GPIO{gpio_pin}: ON for {args.seconds:.1f}s, expected {expected_device}")
            getattr(relay, on_method)()
            time.sleep(max(0.1, args.seconds))
            getattr(relay, off_method)()
            print(f"{relay_input} GPIO{gpio_pin}: OFF")
            time.sleep(max(0.0, args.pause))
    finally:
        relay.cleanup()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Print relay controller internal state and GPIO reading for debugging.

Run on the device where the relays are connected.
"""
import pprint
import time
from app.streaming_agent.gpio.relay_controller import RelayController
def main():
    rc = RelayController()
    rc.start()
    try:
        print("enabled:", rc._enabled)
        print("red_on flag:", rc._red_on)
        print("buzzer_on flag:", rc._buzzer_on)
        print("red_sources:", pprint.pformat(list(rc._red_sources)))
        print("buzzer_sources:", pprint.pformat(list(rc._buzzer_sources)))
        print("detection_source_until:", pprint.pformat(rc._detection_source_until))
        try:
            print("is_security_relays_on():", rc.is_security_relays_on())
        except Exception as exc:
            print("is_security_relays_on() failed:", exc)
    finally:
        rc.cleanup()


if __name__ == "__main__":
    main()

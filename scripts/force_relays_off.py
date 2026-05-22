#!/usr/bin/env python3
"""Force security relays OFF via the RelayController API.

Use this to recover relays that are stuck ON.
"""
import time

from app.streaming_agent.gpio.relay_controller import RelayController


def main():
    rc = RelayController()
    rc.start()
    try:
        print("Initial hardware security relays on:", rc.is_security_relays_on())
        rc.force_security_relays_off()
        # small pause to let GPIO settle
        time.sleep(0.2)
        print("After force, hardware security relays on:", rc.is_security_relays_on())
    finally:
        rc.cleanup()


if __name__ == "__main__":
    main()

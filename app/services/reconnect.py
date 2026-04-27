import os
import time

from app.services.wifi_manager import WifiCommandError, get_wifi_status, is_wifi_connected, start_hotspot, stop_hotspot


CHECK_INTERVAL_SECONDS = int(os.getenv("WIFI_CHECK_INTERVAL_SECONDS", "15"))


def maintain_network_mode() -> None:
    status = get_wifi_status()
    if is_wifi_connected():
        if status["hotspot_active"]:
            stop_hotspot()
        return

    start_hotspot()


def run() -> None:
    while True:
        try:
            maintain_network_mode()
        except WifiCommandError as exc:
            print(f"[wifi-reconnect] {exc}", flush=True)
        except Exception as exc:  # pragma: no cover
            print(f"[wifi-reconnect] unexpected error: {exc}", flush=True)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()

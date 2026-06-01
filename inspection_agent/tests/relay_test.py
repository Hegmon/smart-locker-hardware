from __future__ import annotations

"""Generic relay test utility for any inspection channel."""

from inspection_agent.hardware.relay_controller import RelayController


class RelayTest:
    """Utility helper that can be used to pulse any relay channel."""

    def __init__(self, relay_controller: RelayController) -> None:
        self.relay_controller = relay_controller

    def run(self, channel_name: str, duration_seconds: float = 2.0) -> tuple[bool, str, dict | None]:
        self.relay_controller.pulse(channel_name, duration_seconds=duration_seconds)
        return True, f"{channel_name} relay tested successfully", {"channel": channel_name, "duration_seconds": duration_seconds}

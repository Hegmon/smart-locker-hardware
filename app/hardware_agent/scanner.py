from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from typing import Iterable


class WifiScannerError(RuntimeError):
    pass


# -----------------------------
# DATA MODEL (MQTT SAFE)
# -----------------------------
@dataclass(frozen=True, order=True)
class WifiNetwork:
    ssid: str
    rssi: int
    is_secured: bool

    def to_payload(self) -> dict[str, object]:
        """
        MQTT/Django safe payload format
        """
        return {
            "ssid": self.ssid,
            "rssi": self.rssi,
            "is_secured": self.is_secured,
        }


# -----------------------------
# SCANNER CORE
# -----------------------------
class WifiScanner:
    def __init__(self, interface: str) -> None:
        self.interface = interface
        self._scanner = self._pick_scanner()

    def scan(self) -> list[WifiNetwork]:
        """
        Main scan entrypoint (optimized for repeated calls in agents)
        """
        networks = self._scanner()

        # ---- deduplicate + keep strongest signal ----
        deduped: dict[str, WifiNetwork] = {}

        for network in networks:
            if not network.ssid:
                continue

            existing = deduped.get(network.ssid)

            if existing is None or network.rssi > existing.rssi:
                deduped[network.ssid] = network

        # stable sorting for MQTT payload consistency
        return sorted(deduped.values(), key=lambda x: (-x.rssi, x.ssid))

    # -----------------------------
    # SCANNER SELECTION
    # -----------------------------
    def _pick_scanner(self):
        if shutil.which("nmcli"):
            return self._scan_with_nmcli
        if shutil.which("iw"):
            return self._scan_with_iw
        if shutil.which("iwlist"):
            return self._scan_with_iwlist

        raise WifiScannerError(
            "No WiFi tool found (nmcli/iw/iwlist). Install NetworkManager tools."
        )

    # -----------------------------
    # SAFE EXECUTION WRAPPER
    # -----------------------------
    def _run(self, command: list[str]) -> str:
        result = subprocess.run(command, capture_output=True, text=True)

        if result.returncode != 0:
            raise WifiScannerError(
                f"{' '.join(command)} failed: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

        return result.stdout

    # =========================================================
    # NMCLI (PRIMARY - BEST FOR PRODUCTION RASPBERRY PI)
    # =========================================================
    def _scan_with_nmcli(self) -> list[WifiNetwork]:
        # refresh scan (non-blocking)
        subprocess.run(
            ["nmcli", "device", "wifi", "rescan", "ifname", self.interface],
            capture_output=True,
        )

        output = self._run([
            "nmcli",
            "-t",
            "-f",
            "SSID,SIGNAL,SECURITY",
            "device",
            "wifi",
            "list",
            "ifname",
            self.interface,
        ])

        networks: list[WifiNetwork] = []

        for line in output.splitlines():
            if not line.strip():
                continue

            parts = self._split_nmcli_record(line, 3)

            ssid = parts[0].strip()
            if not ssid:
                continue

            signal = self._safe_int(parts[1], 0)
            security = parts[2].strip()

            networks.append(
                WifiNetwork(
                    ssid=ssid,
                    rssi=self._percent_to_dbm(signal),
                    is_secured=security not in ("", "--"),
                )
            )

        return networks

    # =========================================================
    # IW (FAST LOW LEVEL)
    # =========================================================
    def _scan_with_iw(self) -> list[WifiNetwork]:
        output = self._run(["iw", "dev", self.interface, "scan"])
        return list(self._parse_iw_blocks(output))

    def _parse_iw_blocks(self, output: str) -> Iterable[WifiNetwork]:
        ssid = ""
        signal = None
        secured = False
        in_block = False

        for line in output.splitlines():
            line = line.strip()

            if line.startswith("BSS "):
                if ssid and signal is not None:
                    yield WifiNetwork(ssid, int(signal), secured)

                ssid = ""
                signal = None
                secured = False
                in_block = True
                continue

            if line.startswith("SSID:"):
                ssid = line.split("SSID:")[1].strip()

            elif "signal:" in line:
                match = re.search(r"(-?\d+(?:\.\d+)?)\s*dBm", line)
                if match:
                    signal = float(match.group(1))

            elif "RSN" in line or "WPA" in line:
                secured = True

        if ssid and signal is not None:
            yield WifiNetwork(ssid, int(signal), secured)

    # =========================================================
    # IWLIST (FALLBACK)
    # =========================================================
    def _scan_with_iwlist(self) -> list[WifiNetwork]:
        output = self._run(["iwlist", self.interface, "scan"])
        return list(self._parse_iwlist_blocks(output))

    def _parse_iwlist_blocks(self, output: str) -> Iterable[WifiNetwork]:
        ssid = ""
        signal = None
        secured = False
        in_cell = False

        for line in output.splitlines():
            line = line.strip()

            if "Cell " in line:
                if ssid and signal is not None:
                    yield WifiNetwork(ssid, int(signal), secured)

                ssid = ""
                signal = None
                secured = False
                in_cell = True
                continue

            if "ESSID:" in line:
                ssid = line.split("ESSID:")[1].strip().strip('"')

            elif "Signal level=" in line:
                match = re.search(r"(-?\d+)\s*dBm", line)
                if match:
                    signal = int(match.group(1))

            elif "Encryption key:on" in line:
                secured = True

        if ssid and signal is not None:
            yield WifiNetwork(ssid, int(signal), secured)

    # =========================================================
    # HELPERS
    # =========================================================
    @staticmethod
    def _split_nmcli_record(line: str, expected_parts: int) -> list[str]:
        parts = []
        current = []
        escaped = False

        for c in line:
            if escaped:
                current.append(c)
                escaped = False
                continue

            if c == "\\":
                escaped = True
                continue

            if c == ":" and len(parts) < expected_parts - 1:
                parts.append("".join(current))
                current = []
                continue

            current.append(c)

        parts.append("".join(current))

        while len(parts) < expected_parts:
            parts.append("")

        return parts

    @staticmethod
    def _safe_int(value: str, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return default

    @staticmethod
    def _percent_to_dbm(signal_percent: int) -> int:
        signal_percent = max(0, min(signal_percent, 100))
        return int((signal_percent / 2) - 100)
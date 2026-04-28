from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Iterable


# -----------------------------
# ERROR
# -----------------------------
class WifiScannerError(RuntimeError):
    pass


# -----------------------------
# DATA MODEL
# -----------------------------
@dataclass(frozen=True, order=True)
class WifiNetwork:
    ssid: str
    rssi: int
    is_secured: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "ssid": self.ssid,
            "rssi": self.rssi,
            "is_secured": self.is_secured,
        }


# -----------------------------
# MAIN SCANNER
# -----------------------------
class WifiScanner:
    def __init__(self, interface: str) -> None:
        self.interface = interface
        self._scanner = self._pick_scanner()

    # -----------------------------
    # PUBLIC SCAN
    # -----------------------------
    def scan(self) -> list[WifiNetwork]:
        try:
            networks = self._scanner()
        except Exception as e:
            # 🔥 IMPORTANT: never crash agent loop
            print(f"[WIFI SCANNER ERROR] {e}")
            return []

        if not networks:
            return []

        deduped: dict[str, WifiNetwork] = {}

        for n in networks:
            if not n.ssid:
                continue

            existing = deduped.get(n.ssid)

            if existing is None or n.rssi > existing.rssi:
                deduped[n.ssid] = n

        return sorted(deduped.values(), key=lambda x: (-x.rssi, x.ssid))

    # -----------------------------
    # TOOL SELECTION
    # -----------------------------
    def _pick_scanner(self):
        if shutil.which("nmcli"):
            return self._scan_with_nmcli
        if shutil.which("iw"):
            return self._scan_with_iw
        if shutil.which("iwlist"):
            return self._scan_with_iwlist

        raise WifiScannerError("No WiFi tools found (nmcli/iw/iwlist)")

    # -----------------------------
    # SAFE RUNNER
    # -----------------------------
    def _run(self, cmd: list[str]) -> str:
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            raise WifiScannerError(
                f"{' '.join(cmd)} failed: {result.stderr.strip() or result.stdout.strip()}"
            )

        return result.stdout or ""

    # =========================================================
    # NMCLI (PRIMARY - BEST FOR PI4)
    # =========================================================
    def _scan_with_nmcli(self) -> list[WifiNetwork]:
        # refresh scan (non-blocking)
        subprocess.run(
            ["nmcli", "dev", "wifi", "rescan", "ifname", self.interface],
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

            parts = self._split_nmcli(line, 3)

            ssid = (parts[0] or "").strip()

            # 🔥 skip hidden SSIDs safely
            if not ssid or ssid in ("--", "<hidden>"):
                continue

            signal = self._safe_int(parts[1], 0)
            security = (parts[2] or "").strip()

            networks.append(
                WifiNetwork(
                    ssid=ssid,
                    rssi=self._signal_to_dbm(signal),
                    is_secured=security not in ("", "--"),
                )
            )

        return networks

    # =========================================================
    # IW (LOW LEVEL)
    # =========================================================
    def _scan_with_iw(self) -> list[WifiNetwork]:
        output = self._run(["iw", "dev", self.interface, "scan"])
        return list(self._parse_iw(output))

    def _parse_iw(self, output: str) -> Iterable[WifiNetwork]:
        ssid = ""
        signal = None
        secured = False

        for line in output.splitlines():
            line = line.strip()

            if line.startswith("SSID:"):
                ssid = line.split("SSID:")[1].strip()

            elif "signal:" in line:
                match = re.search(r"(-?\d+(?:\.\d+)?)\s*dBm", line)
                if match:
                    signal = float(match.group(1))

            elif "WPA" in line or "RSN" in line:
                secured = True

            if ssid and signal is not None:
                yield WifiNetwork(ssid, int(signal), secured)
                ssid, signal, secured = "", None, False

    # =========================================================
    # IWLIST (FALLBACK)
    # =========================================================
    def _scan_with_iwlist(self) -> list[WifiNetwork]:
        output = self._run(["iwlist", self.interface, "scan"])
        return list(self._parse_iwlist(output))

    def _parse_iwlist(self, output: str) -> Iterable[WifiNetwork]:
        ssid = ""
        signal = None
        secured = False

        for line in output.splitlines():
            line = line.strip()

            if "ESSID:" in line:
                ssid = line.split("ESSID:")[1].strip().strip('"')

            elif "Signal level=" in line:
                match = re.search(r"(-?\d+)\s*dBm", line)
                if match:
                    signal = int(match.group(1))

            elif "Encryption key:on" in line:
                secured = True

            if ssid and signal is not None:
                yield WifiNetwork(ssid, signal, secured)
                ssid, signal, secured = "", None, False

    # =========================================================
    # HELPERS
    # =========================================================
    @staticmethod
    def _split_nmcli(line: str, expected: int) -> list[str]:
        parts, current, esc = [], [], False

        for c in line:
            if esc:
                current.append(c)
                esc = False
                continue

            if c == "\\":
                esc = True
                continue

            if c == ":" and len(parts) < expected - 1:
                parts.append("".join(current))
                current = []
                continue

            current.append(c)

        parts.append("".join(current))

        while len(parts) < expected:
            parts.append("")

        return parts

    @staticmethod
    def _safe_int(v: str, default: int) -> int:
        try:
            return int(v)
        except Exception:
            return default

    @staticmethod
    def _signal_to_dbm(signal_percent: int) -> int:
        signal_percent = max(0, min(signal_percent, 100))
        return int((signal_percent / 2) - 100)
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from typing import Iterable


class WifiScannerError(RuntimeError):
    pass


@dataclass(frozen=True, order=True)
class WifiNetwork:
    ssid: str
    rssi: int
    is_secured: bool

    def to_payload(self) -> dict[str, object]:
        return asdict(self)


class WifiScanner:
    def __init__(self, interface: str) -> None:
        self.interface = interface
        self._scanner = self._pick_scanner()

    def scan(self) -> list[WifiNetwork]:
        networks = self._scanner()
        deduped: dict[str, WifiNetwork] = {}
        for network in networks:
            if not network.ssid:
                continue
            current = deduped.get(network.ssid)
            if current is None or network.rssi > current.rssi:
                deduped[network.ssid] = network
        return sorted(deduped.values(), key=lambda item: (-item.rssi, item.ssid))

    def _pick_scanner(self):
        if shutil.which("nmcli"):
            return self._scan_with_nmcli
        if shutil.which("iw"):
            return self._scan_with_iw
        if shutil.which("iwlist"):
            return self._scan_with_iwlist
        raise WifiScannerError("No supported WiFi scan command found. Install nmcli, iw, or iwlist.")

    def _run(self, command: list[str]) -> str:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "command failed"
            raise WifiScannerError(f"{' '.join(command)}: {message}")
        return result.stdout

    def _scan_with_nmcli(self) -> list[WifiNetwork]:
        subprocess.run(
            ["nmcli", "device", "wifi", "rescan", "ifname", self.interface],
            capture_output=True,
            text=True,
        )
        output = self._run(
            [
                "nmcli",
                "--colors",
                "no",
                "--escape",
                "yes",
                "-t",
                "-f",
                "SSID,SIGNAL,SECURITY",
                "device",
                "wifi",
                "list",
                "ifname",
                self.interface,
            ]
        )
        networks: list[WifiNetwork] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            parts = self._split_nmcli_record(line, expected_parts=3)
            ssid = parts[0].strip()
            if not ssid:
                continue
            signal = self._safe_int(parts[1], default=0)
            security = parts[2].strip()
            networks.append(
                WifiNetwork(
                    ssid=ssid,
                    rssi=self._percent_to_dbm(signal),
                    is_secured=bool(security and security != "--"),
                )
            )
        return networks

    def _scan_with_iw(self) -> list[WifiNetwork]:
        output = self._run(["iw", "dev", self.interface, "scan"])
        return list(self._parse_iw_blocks(output))

    def _scan_with_iwlist(self) -> list[WifiNetwork]:
        output = self._run(["iwlist", self.interface, "scan"])
        return list(self._parse_iwlist_blocks(output))

    def _parse_iw_blocks(self, output: str) -> Iterable[WifiNetwork]:
        ssid = ""
        signal = None
        secured = False
        in_block = False

        for raw_line in output.splitlines():
            line = raw_line.strip()
            if line.startswith("BSS "):
                if in_block and ssid and signal is not None:
                    yield WifiNetwork(ssid=ssid, rssi=int(signal), is_secured=secured)
                ssid = ""
                signal = None
                secured = False
                in_block = True
                continue
            if line.startswith("SSID:"):
                ssid = line.partition(":")[2].strip()
            elif line.startswith("signal:"):
                match = re.search(r"(-?\d+(?:\.\d+)?)\s*dBm", line)
                if match:
                    signal = float(match.group(1))
            elif line.startswith("RSN:") or line.startswith("WPA:") or "Privacy" in line:
                secured = True

        if in_block and ssid and signal is not None:
            yield WifiNetwork(ssid=ssid, rssi=int(signal), is_secured=secured)

    def _parse_iwlist_blocks(self, output: str) -> Iterable[WifiNetwork]:
        ssid = ""
        signal = None
        secured = False
        in_cell = False

        for raw_line in output.splitlines():
            line = raw_line.strip()
            if line.startswith("Cell "):
                if in_cell and ssid and signal is not None:
                    yield WifiNetwork(ssid=ssid, rssi=int(signal), is_secured=secured)
                ssid = ""
                signal = None
                secured = False
                in_cell = True
                continue
            if "ESSID:" in line:
                ssid = line.partition("ESSID:")[2].strip().strip('"')
            elif "Signal level=" in line:
                match = re.search(r"Signal level=(-?\d+)\s*dBm", line)
                if match:
                    signal = int(match.group(1))
            elif "Encryption key:" in line:
                secured = line.endswith("on")
            elif "IE: WPA" in line or "IE: IEEE 802.11i/WPA2" in line:
                secured = True

        if in_cell and ssid and signal is not None:
            yield WifiNetwork(ssid=ssid, rssi=int(signal), is_secured=secured)

    @staticmethod
    def _split_nmcli_record(line: str, expected_parts: int) -> list[str]:
        parts: list[str] = []
        current: list[str] = []
        escaped = False
        for char in line:
            if escaped:
                current.append(char)
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == ":" and len(parts) < expected_parts - 1:
                parts.append("".join(current))
                current = []
                continue
            current.append(char)
        parts.append("".join(current))
        while len(parts) < expected_parts:
            parts.append("")
        return parts

    @staticmethod
    def _safe_int(value: str, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _percent_to_dbm(signal_percent: int) -> int:
        bounded = max(0, min(signal_percent, 100))
        return int((bounded / 2) - 100)

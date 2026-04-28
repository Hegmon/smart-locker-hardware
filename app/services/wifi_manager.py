from __future__ import annotations
import os
import shlex
import subprocess
import time
from typing import Any
# =========================================================
# CONFIG
# =========================================================
DEFAULT_INTERFACE = os.getenv("WIFI_INTERFACE", "wlan0")

DEFAULT_HOTSPOT_CONNECTION = os.getenv("HOTSPOT_CONNECTION", "SmartLockerHotspot")
DEFAULT_HOTSPOT_SSID = os.getenv("HOTSPOT_SSID", "SmartLocker-Setup")
DEFAULT_HOTSPOT_PASSWORD = os.getenv("HOTSPOT_PASSWORD", "SmartLocker123")


# =========================================================
# EXCEPTION
# =========================================================
class WifiCommandError(RuntimeError):
    pass


# =========================================================
# SAFE EXECUTION LAYER (IMPORTANT FOR MQTT COMMANDS)
# =========================================================
def _run(command: list[str], check: bool = True, timeout: int = 12) -> subprocess.CompletedProcess:
    """
    Hardened subprocess runner (prevents MQTT blocking failures)
    """
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    except subprocess.TimeoutExpired:
        raise WifiCommandError(f"TIMEOUT: {' '.join(command)}")

    if check and result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip()
        raise WifiCommandError(
            f"{' '.join(shlex.quote(c) for c in command)}: {msg}"
        )

    return result


# =========================================================
# CORE HELPERS
# =========================================================
def ensure_wifi_radio() -> None:
    _run(["nmcli", "radio", "wifi", "on"], check=False)


def _wait_for_connection(ssid: str, timeout: int = 15) -> bool:
    """
    MQTT-safe blocking wait with timeout
    """
    start = time.time()

    while time.time() - start < timeout:
        try:
            details = get_connected_wifi_details()
            if details.get("connected_ssid") == ssid:
                return True
        except Exception:
            pass

        time.sleep(1)

    return False


# =========================================================
# WIFI SCAN (MQTT SAFE OUTPUT)
# =========================================================
def scan_wifi() -> list[dict[str, Any]]:
    ensure_wifi_radio()

    _run(["nmcli", "dev", "wifi", "rescan"], check=False)

    result = _run([
        "nmcli",
        "-t",
        "-f",
        "SSID,SIGNAL,SECURITY,IN-USE",
        "dev",
        "wifi",
        "list",
        "ifname",
        DEFAULT_INTERFACE,
    ])

    seen = set()
    networks: list[dict[str, Any]] = []

    for line in result.stdout.splitlines():
        if not line.strip():
            continue

        parts = line.split(":")

        ssid = parts[0].strip()
        if not ssid or ssid in seen:
            continue

        seen.add(ssid)

        signal = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        security = parts[2] if len(parts) > 2 else ""

        networks.append({
            "ssid": ssid,
            "signal": signal,
            "security": security,
            "connected": "*" in parts,
        })

    return sorted(networks, key=lambda x: x["signal"], reverse=True)


# =========================================================
# STATUS
# =========================================================
def get_wifi_status() -> dict[str, Any]:
    ensure_wifi_radio()

    result = _run([
        "nmcli",
        "-t",
        "-f",
        "DEVICE,STATE,CONNECTION",
        "device",
        "status",
    ])

    for line in result.stdout.splitlines():
        parts = line.split(":")

        if parts[0] != DEFAULT_INTERFACE:
            continue

        state = parts[1]
        connection = parts[2] if len(parts) > 2 else ""

        return {
            "interface": DEFAULT_INTERFACE,
            "state": state,
            "connected": state == "connected",
            "connection": connection if connection != "--" else "",
            "hotspot_active": connection == DEFAULT_HOTSPOT_CONNECTION,
        }

    return {
        "interface": DEFAULT_INTERFACE,
        "state": "missing",
        "connected": False,
        "connection": "",
        "hotspot_active": False,
    }


# =========================================================
# CONNECTED WIFI DETAILS
# =========================================================
def get_connected_wifi_details() -> dict[str, Any]:
    ensure_wifi_radio()

    result = _run([
        "nmcli",
        "-t",
        "-f",
        "GENERAL.CONNECTION,GENERAL.STATE,IP4.ADDRESS",
        "device",
        "show",
        DEFAULT_INTERFACE,
    ])

    data = {
        "connected": False,
        "connected_ssid": "",
        "signal_strength": 0,
        "rssi": -100,
        "is_secured": False,
    }

    for line in result.stdout.splitlines():
        if "GENERAL.CONNECTION" in line:
            connection = line.split(":")[1].strip()
            if connection and connection != "--":
                data["connected"] = True
                data["connected_ssid"] = connection

        if "GENERAL.STATE" in line:
            state = line.split(":")[1].strip()
            if "100" in state:
                data["connected"] = True

    # OPTIONAL: better signal fetch
    try:
        signal_result = _run([
            "nmcli",
            "-t",
            "-f",
            "IN-USE,SIGNAL",
            "dev",
            "wifi",
            "list",
            "ifname",
            DEFAULT_INTERFACE,
        ])

        for line in signal_result.stdout.splitlines():
            if line.startswith("*"):
                parts = line.split(":")
                if len(parts) > 1:
                    signal = int(parts[1] or 0)
                    data["signal_strength"] = signal
                    data["rssi"] = int((signal / 2) - 100)
                    break

    except Exception:
        pass

    return data
# =========================================================
# HOTSPOT MODE (DEVICE PROVISIONING)
# =========================================================
def start_hotspot() -> dict[str, Any]:
    ensure_wifi_radio()

    _run(["nmcli", "connection", "down", DEFAULT_HOTSPOT_CONNECTION], check=False)

    existing = _run(
        ["nmcli", "-t", "-f", "NAME", "connection", "show"]
    ).stdout.splitlines()

    if DEFAULT_HOTSPOT_CONNECTION not in existing:
        _run([
            "nmcli",
            "connection",
            "add",
            "type",
            "wifi",
            "ifname",
            DEFAULT_INTERFACE,
            "con-name",
            DEFAULT_HOTSPOT_CONNECTION,
            "ssid",
            DEFAULT_HOTSPOT_SSID,
        ])

        _run([
            "nmcli",
            "connection",
            "modify",
            DEFAULT_HOTSPOT_CONNECTION,
            "802-11-wireless.mode",
            "ap",
            "ipv4.method",
            "shared",
            "wifi-sec.key-mgmt",
            "wpa-psk",
            "wifi-sec.psk",
            DEFAULT_HOTSPOT_PASSWORD,
        ])

    result = _run(["nmcli", "connection", "up", DEFAULT_HOTSPOT_CONNECTION])

    return {
        "status": "hotspot_enabled",
        "ssid": DEFAULT_HOTSPOT_SSID,
        "details": result.stdout.strip(),
    }


def stop_hotspot() -> None:
    _run(["nmcli", "connection", "down", DEFAULT_HOTSPOT_CONNECTION], check=False)


# =========================================================
# CONNECT WIFI (MQTT COMMAND ENTRYPOINT)
# =========================================================
def connect_wifi(ssid: str, password: str) -> dict[str, Any]:
    ensure_wifi_radio()

    stop_hotspot()
    time.sleep(1)  # avoid nmcli race condition

    _run(["nmcli", "connection", "delete", ssid], check=False)

    cmd = ["nmcli", "dev", "wifi", "connect", ssid, "ifname", DEFAULT_INTERFACE]

    if password:
        cmd += ["password", password]

    result = _run(cmd)

    if not _wait_for_connection(ssid):
        raise WifiCommandError(f"Connection verification failed for {ssid}")

    return {
        "status": "connected",
        "ssid": ssid,
        "details": result.stdout.strip(),
        "connection": get_connected_wifi_details(),
    }


# =========================================================
# DISCONNECT
# =========================================================
def disconnect_wifi() -> dict[str, Any]:
    _run(["nmcli", "device", "disconnect", DEFAULT_INTERFACE], check=False)

    hotspot = start_hotspot()

    return {
        "status": "disconnected",
        "hotspot": hotspot,
    }


# =========================================================
# HEALTH CHECK
# =========================================================
def is_wifi_connected() -> bool:
    status = get_wifi_status()
    return status["connected"] and not status["hotspot_active"]
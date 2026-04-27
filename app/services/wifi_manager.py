import os
import shlex
import subprocess
from typing import Any


DEFAULT_INTERFACE = os.getenv("WIFI_INTERFACE", "wlan0")
DEFAULT_HOTSPOT_CONNECTION = os.getenv("HOTSPOT_CONNECTION", "SmartLockerHotspot")
DEFAULT_HOTSPOT_SSID = os.getenv("HOTSPOT_SSID", "SmartLocker-Setup")
DEFAULT_HOTSPOT_PASSWORD = os.getenv("HOTSPOT_PASSWORD", "SmartLocker123")
DEFAULT_HOTSPOT_IP = os.getenv("HOTSPOT_IP", "192.168.4.1/24")


class WifiCommandError(RuntimeError):
    pass


def _run_command(command: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True)
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise WifiCommandError(f"{' '.join(shlex.quote(part) for part in command)}: {message}")
    return result


def _parse_nmcli_table(output: str, columns: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split(":")
        if len(parts) < len(columns):
            parts.extend([""] * (len(columns) - len(parts)))
        row = {columns[index]: parts[index].strip() for index in range(len(columns))}
        rows.append(row)
    return rows


def ensure_wifi_radio() -> None:
    _run_command(["nmcli", "radio", "wifi", "on"])


def scan_wifi() -> list[dict[str, Any]]:
    ensure_wifi_radio()
    _run_command(["nmcli", "dev", "wifi", "rescan"], check=False)
    result = _run_command(
        [
            "nmcli",
            "-t",
            "-f",
            "SSID,SIGNAL,SECURITY,IN-USE",
            "dev",
            "wifi",
            "list",
            "ifname",
            DEFAULT_INTERFACE,
        ]
    )
    networks = _parse_nmcli_table(result.stdout, ["ssid", "signal", "security", "in_use"])
    seen: set[str] = set()
    unique_networks: list[dict[str, Any]] = []
    for network in networks:
        ssid = network["ssid"].strip()
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        unique_networks.append(
            {
                "ssid": ssid,
                "signal": int(network["signal"] or 0),
                "security": network["security"],
                "connected": network["in_use"] == "*",
            }
        )
    return sorted(unique_networks, key=lambda item: item["signal"], reverse=True)


def get_wifi_status() -> dict[str, Any]:
    ensure_wifi_radio()
    result = _run_command(
        [
            "nmcli",
            "-t",
            "-f",
            "DEVICE,TYPE,STATE,CONNECTION",
            "device",
            "status",
        ]
    )
    for row in _parse_nmcli_table(result.stdout, ["device", "type", "state", "connection"]):
        if row["device"] != DEFAULT_INTERFACE:
            continue
        is_connected = row["state"] == "connected"
        connection = row["connection"] if row["connection"] != "--" else ""
        return {
            "interface": row["device"],
            "state": row["state"],
            "connected": is_connected,
            "connection": connection,
            "hotspot_active": connection == DEFAULT_HOTSPOT_CONNECTION,
        }
    return {
        "interface": DEFAULT_INTERFACE,
        "state": "missing",
        "connected": False,
        "connection": "",
        "hotspot_active": False,
    }


def is_wifi_connected() -> bool:
    status = get_wifi_status()
    return bool(status["connected"] and not status["hotspot_active"])


def stop_hotspot() -> None:
    _run_command(["nmcli", "connection", "down", DEFAULT_HOTSPOT_CONNECTION], check=False)


def ensure_hotspot_connection() -> None:
    existing_connections = _run_command(
        ["nmcli", "-t", "-f", "NAME", "connection", "show"]
    ).stdout.splitlines()
    if DEFAULT_HOTSPOT_CONNECTION in existing_connections:
        return

    _run_command(
        [
            "nmcli",
            "connection",
            "add",
            "type",
            "wifi",
            "ifname",
            DEFAULT_INTERFACE,
            "con-name",
            DEFAULT_HOTSPOT_CONNECTION,
            "autoconnect",
            "no",
            "ssid",
            DEFAULT_HOTSPOT_SSID,
        ]
    )
    _run_command(
        [
            "nmcli",
            "connection",
            "modify",
            DEFAULT_HOTSPOT_CONNECTION,
            "802-11-wireless.mode",
            "ap",
            "802-11-wireless.band",
            "bg",
            "ipv4.method",
            "shared",
            "ipv4.addresses",
            DEFAULT_HOTSPOT_IP,
            "wifi-sec.key-mgmt",
            "wpa-psk",
            "wifi-sec.psk",
            DEFAULT_HOTSPOT_PASSWORD,
        ]
    )


def start_hotspot() -> dict[str, Any]:
    ensure_wifi_radio()
    ensure_hotspot_connection()
    result = _run_command(["nmcli", "connection", "up", DEFAULT_HOTSPOT_CONNECTION])
    return {
        "status": "hotspot_enabled",
        "ssid": DEFAULT_HOTSPOT_SSID,
        "connection": DEFAULT_HOTSPOT_CONNECTION,
        "details": result.stdout.strip(),
    }


def connect_wifi(ssid: str, password: str) -> dict[str, Any]:
    ensure_wifi_radio()
    stop_hotspot()

    _run_command(["nmcli", "connection", "delete", ssid], check=False)

    command = ["nmcli", "dev", "wifi", "connect", ssid, "ifname", DEFAULT_INTERFACE]
    if password:
        command.extend(["password", password])

    result = _run_command(command)
    return {
        "status": "connected",
        "ssid": ssid,
        "details": result.stdout.strip(),
    }


def disconnect_wifi() -> dict[str, Any]:
    _run_command(["nmcli", "device", "disconnect", DEFAULT_INTERFACE], check=False)
    hotspot = start_hotspot()
    return {"status": "disconnected", "hotspot": hotspot}

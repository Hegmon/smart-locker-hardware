from __future__ import annotations
import os
import shlex
import time
import threading
from typing import Any
import subprocess

from app.deployment.runtime_config import get_bool_setting, get_int_setting, get_str_setting
from app.utils.logger import get_logger

#========================================================================================
# CONFIG
#======================================================================================
DEFAULT_INTERFACE = get_str_setting("WIFI_INTERFACE", "wlan0")

DEFAULT_HOTSPOT_CONNECTION = get_str_setting("HOTSPOT_CONNECTION", "SmartLockerHotspot")
DEFAULT_HOTSPOT_SSID = get_str_setting("HOTSPOT_SSID", "SmartLocker-Setup")
DEFAULT_HOTSPOT_PASSWORD = get_str_setting("HOTSPOT_PASSWORD", "SmartLocker123")
DEFAULT_WIFI_CONNECT_TIMEOUT_SECONDS = get_int_setting("QBOX_WIFI_AGENT_WIFI_CONNECT_TIMEOUT_SECONDS", 30)
ALLOW_NON_ROOT_NMCLI = get_bool_setting("ALLOW_NON_ROOT_NMCLI", False)
_WIFI_LOCK = threading.RLock()
logger = get_logger(__name__)

#=================================================================================
# EXCEPTION
#=============================================================================
class WifiCommandError(RuntimeError):
    pass

#==============================================================================
# SAFE EXECUTION
#==============================================================================
def _run(
    command: list[str],
    check: bool = True,
    timeout: int = 12,
    *,
    require_root: bool = False,
) -> subprocess.CompletedProcess[str]:
    effective_command = _noninteractive_command(command, require_root=require_root)
    try:
        result = subprocess.run(
            effective_command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )
    except subprocess.TimeoutExpired:
        raise WifiCommandError(f"Timeout: {_redact_command(command)}")
    except subprocess.CalledProcessError as exc:
        output = (exc.stderr or exc.stdout or "").strip()
        safe_command = _redact_command(command)
        if output:
            raise WifiCommandError(f"{safe_command}: {output}") from exc
        raise WifiCommandError(f"{safe_command}: exited with status {exc.returncode}") from exc

    if check and result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip()
        safe_command = _redact_command(command)
        raise WifiCommandError(f"{safe_command}: {msg}")

    return result


def _nmcli(
    args: list[str],
    *,
    check: bool = True,
    timeout: int = 12,
    require_root: bool = False,
) -> subprocess.CompletedProcess[str]:
    wait_seconds = max(1, min(timeout, DEFAULT_WIFI_CONNECT_TIMEOUT_SECONDS))
    return _run(
        ["nmcli", "--wait", str(wait_seconds), *args],
        check=check,
        timeout=timeout + 3,
        require_root=require_root,
    )


def _noninteractive_command(command: list[str], *, require_root: bool) -> list[str]:
    if not require_root or not command or command[0] != "nmcli":
        return command
    if ALLOW_NON_ROOT_NMCLI or getattr(os, "geteuid", lambda: 0)() == 0:
        return command

    return command


def _redact_command(command: list[str]) -> str:
    redacted = list(command)
    for index, value in enumerate(redacted[:-1]):
        if value.lower() in {"password", "psk", "wifi-sec.psk"}:
            redacted[index + 1] = "********"
    return " ".join(shlex.quote(c) for c in redacted)


# =========================================================
# RETRY WRAPPER
# =========================================================
def _retry(fn, retires=2, delay=1):
    last_error = None
    for i in range(retires):
        try:
            return fn()
        except Exception as e:
            last_error = e
            time.sleep(delay)

    raise last_error


#=================================================================
# CORE
#=================================================================

def ensure_wifi_radio() -> None:
    try:
        _nmcli(["radio", "wifi", "on"], check=False, timeout=5, require_root=True)
    except WifiCommandError as exc:
        logger.debug("WiFi radio enable skipped: %s", exc)


def _wait_for_connection(ssid: str, timeout: int = 15) -> bool:
    start = time.time()

    while time.time() - start < timeout:
        try:
            if get_connected_wifi_details().get("connected_ssid") == ssid:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


#==========================================================================
# STATUS
#=========================================================================
def get_wifi_status() -> dict[str, Any]:
    ensure_wifi_radio()

    result = _nmcli(["-t", "-f", "DEVICE,STATE,CONNECTION", "device", "status"], timeout=5)
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
# CONNECTED DETAILS
# =========================================================
def get_connected_wifi_details() -> dict[str, Any]:
    ensure_wifi_radio()

    data = {
        "connected": False,
        "connected_ssid": "",
        "signal_strength": 0,
        "rssi": 0,
        "is_secured": False,
        "ip_address": "",
    }
    try:
        result = _nmcli([
            "-t",
            "-f",
            "GENERAL.CONNECTION,GENERAL.STATE,IP4.ADDRESS[1]",
            "device",
            "show",
            DEFAULT_INTERFACE,
        ])
        for line in result.stdout.splitlines():
            if "GENERAL.CONNECTION" in line:
                parts = line.split(":", 1)
                ssid = (parts[1].strip() if len(parts) > 1 else "")
                if ssid and ssid != "--":
                    data["connected"] = True
                    data["connected_ssid"] = ssid
            elif "IP4.ADDRESS" in line:
                parts = line.split(":", 1)
                address = (parts[1].strip() if len(parts) > 1 else "")
                if address:
                    data["ip_address"] = address.split("/", 1)[0]

        signal = _nmcli([
            "-t",
            "-f",
            "IN-USE,SIGNAL",
            "dev",
            "wifi",
            "list",
            "ifname",
            DEFAULT_INTERFACE,
        ])
        for line in signal.stdout.splitlines():
            if line.startswith("*"):
                s = int(line.split(":")[1] or 0)
                data["signal_strength"] = s
                data["rssi"] = int((s / 2) - 100)
                break
    except Exception:
        return data

    return data


def list_saved_wifi_networks() -> list[str]:
    ensure_wifi_radio()
    result = _nmcli(["-t", "-f", "NAME,TYPE", "connection", "show"], timeout=5)
    saved_ssids: list[str] = []
    seen: set[str] = set()

    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        name, connection_type = parts
        ssid = name.strip()
        if connection_type.strip() != "802-11-wireless":
            continue
        if not ssid or ssid == DEFAULT_HOTSPOT_CONNECTION or ssid in seen:
            continue
        seen.add(ssid)
        saved_ssids.append(ssid)

    logger.info("Saved WiFi profiles detected: %s", saved_ssids)
    return saved_ssids


def _saved_profile_exists(ssid: str) -> bool:
    if not ssid:
        return False
    result = _nmcli(["-t", "-f", "NAME,TYPE", "connection", "show"], timeout=5)
    for line in result.stdout.splitlines():
        name, _, connection_type = line.partition(":")
        if name == ssid and connection_type.strip() == "802-11-wireless":
            return True
    return False


# =========================================================
# HOTSPOT START
# =========================================================
def start_hotspot() -> dict[str, Any]:
    def _start():
        ensure_wifi_radio()
        if not _saved_profile_exists(DEFAULT_HOTSPOT_CONNECTION):
            _nmcli([
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
            ], require_root=True)
        _nmcli([
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
        ], require_root=True)

        result = _nmcli(["connection", "up", DEFAULT_HOTSPOT_CONNECTION], timeout=DEFAULT_WIFI_CONNECT_TIMEOUT_SECONDS, require_root=True)
        return {
            "status": "hotspot_enabled",
            "ssid": DEFAULT_HOTSPOT_SSID,
            "details": result.stdout.strip(),
        }

    with _WIFI_LOCK:
        return _retry(_start)


def stop_hotspot() -> None:
    _nmcli(["connection", "down", DEFAULT_HOTSPOT_CONNECTION], check=False, timeout=8, require_root=True)


# =========================================================
# RECONNECT
# =========================================================
def reconnect_saved_wifi(ssid: str) -> dict[str, Any]:
    def _connect():
        ensure_wifi_radio()
        stop_hotspot()

        if not _saved_profile_exists(ssid):
            raise WifiCommandError(f"Reconnect failed:{ssid}: saved profile not found")

        result = _nmcli(
            ["connection", "up", "id", ssid, "ifname", DEFAULT_INTERFACE],
            timeout=DEFAULT_WIFI_CONNECT_TIMEOUT_SECONDS,
            require_root=True,
        )
        logger.info("Saved WiFi reconnect command succeeded for %s", ssid)

        if not _wait_for_connection(ssid, timeout=min(10, DEFAULT_WIFI_CONNECT_TIMEOUT_SECONDS)):
            raise WifiCommandError(f"Reconnect failed:{ssid}")
        return {
            "status": "reconnected",
            "ssid": ssid,
            "details": result.stdout.strip(),
            "connection": get_connected_wifi_details(),
        }

    with _WIFI_LOCK:
        return _retry(_connect, retires=1)


# =========================================================
# CONNECT WIFI
# =========================================================
def connect_wifi(ssid: str, password: str) -> dict[str, Any]:
    def _connect():
        ensure_wifi_radio()

        stop_hotspot()
        time.sleep(0.3)

        if _saved_profile_exists(ssid):
            if password:
                _nmcli(
                    [
                        "connection",
                        "modify",
                        "id",
                        ssid,
                        "wifi-sec.key-mgmt",
                        "wpa-psk",
                        "wifi-sec.psk",
                        password,
                        "connection.autoconnect",
                        "yes",
                    ],
                    timeout=8,
                    require_root=True,
                )
            result = _nmcli(
                ["connection", "up", "id", ssid, "ifname", DEFAULT_INTERFACE],
                timeout=DEFAULT_WIFI_CONNECT_TIMEOUT_SECONDS,
                require_root=True,
            )
        else:
            cmd = ["dev", "wifi", "connect", ssid, "ifname", DEFAULT_INTERFACE, "name", ssid]
            if password:
                cmd += ["password", password]
            result = _nmcli(cmd, timeout=DEFAULT_WIFI_CONNECT_TIMEOUT_SECONDS, require_root=True)
        if not _wait_for_connection(ssid, timeout=min(10, DEFAULT_WIFI_CONNECT_TIMEOUT_SECONDS)):
            raise WifiCommandError(f"Connection failed:{ssid}")
        return {
            "status": "connected",
            "ssid": ssid,
            "details": result.stdout.strip(),
            "connection": get_connected_wifi_details(),
        }

    with _WIFI_LOCK:
        return _retry(_connect, retires=1)


# =================================================================================
# DISCONNECT
# =================================================================================
def disconnect_wifi() -> dict[str, Any]:
    with _WIFI_LOCK:
        _nmcli(["device", "disconnect", DEFAULT_INTERFACE], check=False, timeout=8, require_root=True)
        hotspot = start_hotspot()
        return {"status": "disconnected", "hotspot": hotspot}


# ======================================================================================
# HEALTH CHECK
# ======================================================================================
def is_wifi_connectd() -> bool:
    status = get_wifi_status()
    return status["connected"] and not status["hotspot_active"]


def is_wifi_connected() -> bool:
    return is_wifi_connectd()


# =========================================================
# SCAN NETWORKS
# =========================================================
def scan_wifi() -> list[dict[str, Any]]:
    ensure_wifi_radio()
    result = _nmcli([
        "-t",
        "-f",
        "SSID,SIGNAL,SECURITY",
        "device",
        "wifi",
        "list",
        "ifname",
        DEFAULT_INTERFACE,
    ])
    networks: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(":", 2)
        ssid = parts[0] if len(parts) > 0 else ""
        if not ssid or ssid in ("--", "<hidden>"):
            continue
        try:
            signal = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        except Exception:
            signal = 0
        security = parts[2] if len(parts) > 2 else ""
        networks.append({
            "ssid": ssid,
            "rssi": int((signal / 2) - 100),
            "secured": security not in ("", "--"),
        })
    return networks


def scan_hotspot() -> list[dict[str, Any]]:
    # Provide a simple listing for hotspot-like connections (active/available)
    try:
        result = _nmcli(["-t", "-f", "NAME,TYPE,DEVICE", "connection", "show"], timeout=5)
        hotspots = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(":")
            name = parts[0] if parts else ""
            ctype = parts[1] if len(parts) > 1 else ""
            device = parts[2] if len(parts) > 2 else ""
            if ctype == "wifi":
                hotspots.append({"name": name, "device": device})
        return hotspots
    except Exception:
        return []

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


class WifiAuthenticationError(WifiCommandError):
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


def _split_nmcli(line: str, expected: int) -> list[str]:
    parts, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == ":" and len(parts) < expected - 1:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    while len(parts) < expected:
        parts.append("")
    return parts


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


def _active_wifi_details() -> dict[str, Any]:
    result = _nmcli([
        "-t",
        "-f",
        "IN-USE,SSID,SIGNAL,SECURITY",
        "dev",
        "wifi",
        "list",
        "ifname",
        DEFAULT_INTERFACE,
    ], timeout=8)

    for line in result.stdout.splitlines():
        parts = _split_nmcli(line, 4)
        if parts[0] != "*":
            continue
        ssid = parts[1].strip()
        signal = 0
        try:
            signal = int(parts[2] or 0)
        except ValueError:
            signal = 0
        security = parts[3].strip()
        return {
            "ssid": ssid,
            "signal_strength": signal,
            "rssi": int((signal / 2) - 100),
            "is_secured": security not in ("", "--"),
        }
    return {}


def _connection_summary() -> dict[str, str]:
    summary = {
        "profile": "",
        "state": "",
        "ip_address": "",
    }
    result = _nmcli([
        "-t",
        "-f",
        "GENERAL.CONNECTION,GENERAL.STATE,IP4.ADDRESS[1]",
        "device",
        "show",
        DEFAULT_INTERFACE,
    ], timeout=8)
    for line in result.stdout.splitlines():
        if "GENERAL.CONNECTION" in line:
            _, _, value = line.partition(":")
            summary["profile"] = value.strip()
        elif "GENERAL.STATE" in line:
            _, _, value = line.partition(":")
            summary["state"] = value.strip()
        elif "IP4.ADDRESS" in line:
            _, _, value = line.partition(":")
            summary["ip_address"] = value.strip().split("/", 1)[0]
    return summary


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
        "connection_profile": "",
        "device_state": "",
        "signal_strength": 0,
        "rssi": 0,
        "is_secured": False,
        "ip_address": "",
    }
    try:
        summary = _connection_summary()
        active = _active_wifi_details()
        data["connection_profile"] = summary["profile"] if summary["profile"] != "--" else ""
        data["device_state"] = summary["state"]
        data["ip_address"] = summary["ip_address"]
        if active.get("ssid") and summary["state"].startswith("100"):
            data["connected"] = True
            data["connected_ssid"] = active["ssid"]
            data["signal_strength"] = active["signal_strength"]
            data["rssi"] = active["rssi"]
            data["is_secured"] = active["is_secured"]
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


def _delete_saved_profile(ssid: str) -> None:
    if not ssid:
        return
    for _ in range(3):
        if not _saved_profile_exists(ssid):
            return
        _nmcli(["connection", "down", "id", ssid], check=False, timeout=5, require_root=True)
        _nmcli(["connection", "delete", "id", ssid], check=False, timeout=8, require_root=True)


def _cancel_wifi_activation() -> None:
    _nmcli(["device", "disconnect", DEFAULT_INTERFACE], check=False, timeout=5, require_root=True)
    ensure_wifi_radio()


def _disable_profile_autoconnect(ssid: str) -> None:
    if _saved_profile_exists(ssid):
        _nmcli(
            ["connection", "modify", "id", ssid, "connection.autoconnect", "no"],
            check=False,
            timeout=5,
            require_root=True,
        )


def _create_wifi_profile(ssid: str, password: str) -> None:
    _delete_saved_profile(ssid)
    _nmcli([
        "connection",
        "add",
        "type",
        "wifi",
        "ifname",
        DEFAULT_INTERFACE,
        "con-name",
        ssid,
        "ssid",
        ssid,
    ], timeout=8, require_root=True)
    _nmcli([
        "connection",
        "modify",
        "id",
        ssid,
        "802-11-wireless.hidden",
        "no",
        "connection.autoconnect",
        "no",
        "ipv4.method",
        "auto",
        "ipv6.method",
        "auto",
    ], timeout=8, require_root=True)
    if password:
        _nmcli([
            "connection",
            "modify",
            "id",
            ssid,
            "wifi-sec.key-mgmt",
            "wpa-psk",
            "802-11-wireless-security.key-mgmt",
            "wpa-psk",
            "wifi-sec.psk",
            password,
            "802-11-wireless-security.psk",
            password,
            "wifi-sec.psk-flags",
            "0",
            "802-11-wireless-security.psk-flags",
            "0",
        ], timeout=8, require_root=True)


def _raise_classified_wifi_error(ssid: str, error: Exception) -> None:
    text = str(error)
    lowered = text.lower()
    if (
        "secrets were required" in lowered
        or "no secrets" in lowered
        or "802-11-wireless-security.psk" in lowered
        or "wifi-sec.psk" in lowered
        or "802-11-wireless-security" in lowered
        or "wrong password" in lowered
        or "wrong or missing" in lowered
        or "password" in lowered and "not given" in lowered
        or "auth" in lowered and "fail" in lowered
        or "secrets" in lowered
    ):
        raise WifiAuthenticationError(f"Authentication failed for {ssid}: wrong or missing WiFi password") from error
    raise WifiCommandError(text) from error


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

        try:
            result = _nmcli(
                ["connection", "up", "id", ssid, "ifname", DEFAULT_INTERFACE],
                timeout=DEFAULT_WIFI_CONNECT_TIMEOUT_SECONDS,
                require_root=True,
            )
            logger.info("Saved WiFi reconnect command succeeded for %s", ssid)

            if not _wait_for_connection(ssid, timeout=min(20, DEFAULT_WIFI_CONNECT_TIMEOUT_SECONDS)):
                details = get_connected_wifi_details()
                logger.warning("Reconnect wait failed for %s; current WiFi details: %s", ssid, details)
                raise WifiCommandError(f"Reconnect failed:{ssid}")
        except Exception as exc:
            _disable_profile_autoconnect(ssid)
            _cancel_wifi_activation()
            _raise_classified_wifi_error(ssid, exc)
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

        try:
            if password:
                _create_wifi_profile(ssid, password)
                verify = _nmcli(
                    ["-g", "802-11-wireless-security.psk-flags", "connection", "show", "id", ssid],
                    check=False,
                    timeout=5,
                    require_root=True,
                )
                if verify.returncode != 0 or verify.stdout.strip() not in {"0", ""}:
                    raise WifiAuthenticationError(f"Authentication failed for {ssid}: WiFi password was not stored")
            elif not _saved_profile_exists(ssid):
                raise WifiAuthenticationError(f"Authentication failed for {ssid}: password required for new network")

            result = _nmcli(
                ["connection", "up", "id", ssid, "ifname", DEFAULT_INTERFACE],
                timeout=DEFAULT_WIFI_CONNECT_TIMEOUT_SECONDS,
                require_root=True,
            )

            if not _wait_for_connection(ssid, timeout=min(20, DEFAULT_WIFI_CONNECT_TIMEOUT_SECONDS)):
                details = get_connected_wifi_details()
                logger.warning("Connection wait failed for %s; current WiFi details: %s", ssid, details)
                raise WifiCommandError(f"Connection failed:{ssid}")
            _nmcli(
                ["connection", "modify", "id", ssid, "connection.autoconnect", "yes"],
                check=False,
                timeout=5,
                require_root=True,
            )
        except WifiAuthenticationError:
            _cancel_wifi_activation()
            _delete_saved_profile(ssid)
            raise
        except Exception as exc:
            _cancel_wifi_activation()
            if password:
                _delete_saved_profile(ssid)
            _raise_classified_wifi_error(ssid, exc)

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

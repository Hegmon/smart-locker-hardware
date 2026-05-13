from __future__ import annotations

from typing import Any


def build_wifi_connect_success(ssid: str, connected: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "SUCCESS",
        "ssid": ssid,
        "connected_ssid": connected.get("connected_ssid") or ssid,
        "message": "Connected",
        "details": {
            "ip": connected.get("ip_address") or "",
            "rssi": connected.get("rssi", 0),
            "signal_strength": connected.get("signal_strength", 0),
        },
    }


def build_wifi_connect_failure(
    ssid: str,
    error: str,
    *,
    fallback_ssid: str = "",
) -> dict[str, Any]:
    reason = wifi_failure_reason(error)
    details: dict[str, Any] = {"reason": reason}
    if fallback_ssid:
        details["fallback_ssid"] = fallback_ssid
    return {
        "status": "FAILED",
        "ssid": ssid,
        "message": wifi_failure_message(reason),
        "details": details,
    }


def wifi_failure_reason(error: str) -> str:
    lowered = str(error or "").lower()
    if "authentication failed" in lowered or "wrong or missing" in lowered or "secrets" in lowered:
        return "auth_failed"
    if "internet validation failed" in lowered or "internet" in lowered:
        return "internet_unavailable"
    if "timeout" in lowered:
        return "timeout"
    if "not found" in lowered:
        return "profile_not_found"
    return "network_unavailable"


def wifi_failure_message(reason: str) -> str:
    if reason == "auth_failed":
        return "Authentication failed or network unavailable"
    if reason == "internet_unavailable":
        return "Connected but internet validation failed"
    if reason == "timeout":
        return "WiFi connection timed out"
    return "Authentication failed or network unavailable"

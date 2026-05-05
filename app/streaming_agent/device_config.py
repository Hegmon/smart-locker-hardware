"""
Device and stream configuration helpers.
Reads identity from app/config/backend_device.json and optional overrides
from /etc/qbox-device.conf.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from app.deployment.device_identity import ensure_device_id
from app.deployment.runtime_config import DEFAULT_CONFIG_JSON, get_str_setting

# Optional override file (takes precedence)
DEVICE_CONFIG_PATH = Path("/etc/qbox-device.conf")
SMARTLOCKER_CONFIG_PATH = DEFAULT_CONFIG_JSON

# Path to backend_device.json (relative to this file)
# device_config.py is in app/streaming_agent/
# backend_device.json is in app/config/
STATE_FILE = Path(__file__).resolve().parent.parent / "config" / "backend_device.json"


def _load_backend_json() -> dict:
    """Load backend_device.json safely"""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] Failed to read backend state from {STATE_FILE}: {e}")
    return {}


def _load_override() -> dict[str, str]:
    """Load optional override from /etc/qbox-device.conf"""
    result: dict[str, str] = {}

    if DEVICE_CONFIG_PATH.exists():
        content = DEVICE_CONFIG_PATH.read_text(encoding="utf-8").strip()

        try:
            loaded = json.loads(content)
            if isinstance(loaded, dict):
                for key, value in loaded.items():
                    result[str(key).upper()] = str(value)
        except json.JSONDecodeError:
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                result[key.strip().upper()] = value.strip()

    try:
        loaded = json.loads(SMARTLOCKER_CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        loaded = {}

    if isinstance(loaded, dict):
        for section, values in loaded.items():
            if isinstance(values, dict):
                for key, value in values.items():
                    result[f"{section}_{key}".upper()] = str(value)
            else:
                result[str(section).upper()] = str(values)
    return result


def get_optional_config(key: str, default: str = "") -> str:
    """Read an optional config value from override or backend state."""
    override = _load_override()
    state = _load_backend_json()

    value = (
        os.getenv(key.upper())
        or os.getenv(key.lower())
        or
        get_str_setting(key.upper(), "")
        or
        override.get(key.upper())
        or override.get(key.lower())
        or state.get(key)
        or state.get(key.upper())
    )
    if value is None:
        return default
    return str(value).strip()


def load_device_id() -> str:
    """
    Load device_id from backend state or config override.
    
    Priority:
    1. /etc/qbox-device.conf (override)
    2. app/config/backend_device.json (Django registration)
    
    Raises RuntimeError if not found.
    """
    override = _load_override()
    device_id = override.get("DEVICE_ID") or override.get("device_id")
    if device_id:
        return str(device_id).strip()

    state = _load_backend_json()
    device_id = state.get("device_id") or state.get("DEVICE_ID")
    if device_id:
        return str(device_id).strip()

    return ensure_device_id()


def get_device_config() -> dict[str, str]:
    """
    Get full device configuration.
    
    Returns:
        {"device_id": str, "device_uuid": str, ...optional stream config}
    """
    override = _load_override()
    state = _load_backend_json()
    
    # Resolve device_id
    device_id = (
        override.get("DEVICE_ID") or override.get("device_id")
        or state.get("device_id") or state.get("DEVICE_ID")
    )
    if not device_id:
        device_id = ensure_device_id()
    
    # Resolve device_uuid
    device_uuid = (
        override.get("DEVICE_UUID") or override.get("device_uuid")
        or state.get("device_uuid") or state.get("DEVICE_UUID")
    ) or ""
    
    return {
        "device_id": str(device_id).strip(),
        "device_uuid": str(device_uuid).strip(),
        "stream_public_base_url": get_optional_config("STREAM_PUBLIC_BASE_URL"),
        "stream_public_host": get_optional_config("STREAM_PUBLIC_HOST"),
        "stream_public_scheme": get_optional_config("STREAM_PUBLIC_SCHEME"),
        "stream_public_port": get_optional_config("STREAM_PUBLIC_PORT"),
        "stream_public_base_path": get_optional_config("STREAM_PUBLIC_BASE_PATH"),
        "mediamtx_host": get_optional_config("MEDIAMTX_HOST"),
        "mediamtx_rtsp_port": get_optional_config("MEDIAMTX_RTSP_PORT"),
        "mediamtx_hls_host": get_optional_config("MEDIAMTX_HLS_HOST"),
        "mediamtx_hls_port": get_optional_config("MEDIAMTX_HLS_PORT"),
    }

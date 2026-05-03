"""
Device Identity Configuration
Loads device_id and device_uuid from the existing backend state.
Falls back to /etc/qbox-device.conf for override if provided.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


# Optional override file (takes precedence)
DEVICE_CONFIG_PATH = Path("/etc/qbox-device.conf")


def _load_override() -> dict[str, str]:
    """Load optional override from /etc/qbox-device.conf"""
    if not DEVICE_CONFIG_PATH.exists():
        return {}
    
    content = DEVICE_CONFIG_PATH.read_text(encoding="utf-8").strip()
    
    # Try JSON first
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    
    # Fallback to key=value parsing
    result = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip().upper()] = value.strip()
    return result


def load_backend_state_safe() -> dict:
    """Safely load existing backend_device.json (registered by Django)"""
    try:
        # Import here to avoid potential circular import
        from app.services.backend_state import load_backend_state
        return load_backend_state()
    except Exception:
        return {}


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
    
    state = load_backend_state_safe()
    device_id = state.get("device_id") or state.get("DEVICE_ID")
    if device_id:
        return str(device_id).strip()
    
    raise RuntimeError(
        "device_id not found. Register device with Django backend first "
        "(creates app/config/backend_device.json) OR create /etc/qbox-device.conf "
        "with DEVICE_ID=QBOX-001"
    )


def get_device_config() -> dict[str, str]:
    """
    Get full device configuration.
    
    Returns:
        {"device_id": str, "device_uuid": str}
    """
    override = _load_override()
    state = load_backend_state_safe()
    
    # Resolve device_id
    device_id = (
        override.get("DEVICE_ID") or override.get("device_id")
        or state.get("device_id") or state.get("DEVICE_ID")
    )
    if not device_id:
        raise RuntimeError(
            "device_id not found. Ensure backend registration completed "
            "or set /etc/qbox-device.conf"
        )
    
    # Resolve device_uuid
    device_uuid = (
        override.get("DEVICE_UUID") or override.get("device_uuid")
        or state.get("device_uuid") or state.get("DEVICE_UUID")
    ) or ""
    
    return {
        "device_id": str(device_id).strip(),
        "device_uuid": str(device_uuid).strip(),
    }

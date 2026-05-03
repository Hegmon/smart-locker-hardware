"""
Device Identity Configuration
Loads device_id from /etc/qbox-device.conf (required for stream URLs)
Never hardcode device_id - it must be read from device configuration.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


DEVICE_CONFIG_PATH = Path("/etc/qbox-device.conf")


def load_device_id() -> str:
    """
    Load device_id from /etc/qbox-device.conf.
    
    Supported formats:
    1. JSON: {"device_id": "QBOX-001"}
    2. Key=value: DEVICE_ID=QBOX-001
    
    Raises:
        RuntimeError: if config file not found or device_id missing
    """
    if not DEVICE_CONFIG_PATH.exists():
        raise RuntimeError(
            f"Device configuration not found at {DEVICE_CONFIG_PATH}. "
            "This file is required for streaming. It should contain device_id=..."
        )
    
    content = DEVICE_CONFIG_PATH.read_text(encoding="utf-8").strip()
    
    # Try JSON first
    try:
        data = json.loads(content)
        device_id = data.get("device_id") or data.get("DEVICE_ID")
        if device_id:
            return str(device_id).strip()
    except json.JSONDecodeError:
        pass
    
    # Fallback to key=value parsing
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip().upper()
            if key in ("DEVICE_ID", "DEVICEID", "QBOX_DEVICE_ID"):
                return value.strip()
    
    raise ValueError(
        f"device_id not found in {DEVICE_CONFIG_PATH}. "
        "Add a line like: DEVICE_ID=QBOX-001"
    )


def get_device_config() -> dict[str, str]:
    """
    Load full device config from /etc/qbox-device.conf.
    Returns dict with device_id, and optional device_uuid.
    """
    config = {"device_id": load_device_id()}
    
    if DEVICE_CONFIG_PATH.exists():
        content = DEVICE_CONFIG_PATH.read_text(encoding="utf-8").strip()
        try:
            data = json.loads(content)
            config["device_uuid"] = data.get("device_uuid") or data.get("DEVICE_UUID")
        except json.JSONDecodeError:
            # Also check key=value for device_uuid
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip().upper()
                    if key in ("DEVICE_UUID", "UUID"):
                        config["device_uuid"] = value.strip()
    
    # Fallback: try backend state for device_uuid
    if not config.get("device_uuid"):
        try:
            from app.services.backend_state import load_backend_state
            state = load_backend_state()
            config["device_uuid"] = state.get("device_uuid", "")
        except Exception:
            config["device_uuid"] = ""
    
    return config

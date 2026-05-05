from __future__ import annotations

import uuid
from pathlib import Path

from app.deployment.runtime_config import get_path_setting


DEFAULT_DEVICE_ID_FILE = "/etc/smartlocker/device_id"


def device_id_file() -> Path:
    return get_path_setting("SMARTLOCKER_DEVICE_ID_FILE", DEFAULT_DEVICE_ID_FILE)


def _generate_device_id() -> str:
    node = uuid.getnode()
    if (node >> 40) % 2 == 0:
        return f"SL-{node:012X}"
    return f"SL-{uuid.uuid4().hex[:12].upper()}"


def read_device_id() -> str | None:
    path = device_id_file()
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return value or None


def ensure_device_id() -> str:
    existing = read_device_id()
    if existing:
        return existing

    generated = _generate_device_id()
    path = device_id_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{generated}\n", encoding="utf-8")
    return generated

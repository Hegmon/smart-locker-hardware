import json
import os
from pathlib import Path
from typing import Any

from app.deployment.runtime_config import get_path_setting

DEFAULT_STATE_FILE = Path(__file__).resolve().parent.parent / "config" / "backend_device.json"
SYSTEM_STATE_FILE = Path("/var/lib/smartlocker/backend_device.json")

STATE_FILE = get_path_setting(
    "SMARTLOCKER_BACKEND_STATE_FILE",
    str(DEFAULT_STATE_FILE),
)


def _state_read_paths() -> list[Path]:
    paths = [STATE_FILE]
    if "SMARTLOCKER_BACKEND_STATE_FILE" not in os.environ:
        paths.append(SYSTEM_STATE_FILE)
    paths.append(DEFAULT_STATE_FILE)

    unique_paths: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique_paths.append(path)
    return unique_paths


def load_backend_state() -> dict[str, Any]:
    for path in _state_read_paths():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            return loaded
    return {}


def save_backend_state(data: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

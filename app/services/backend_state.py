import json
from pathlib import Path
from typing import Any

from app.deployment.runtime_config import get_path_setting

STATE_FILE = get_path_setting(
    "SMARTLOCKER_BACKEND_STATE_FILE",
    str(Path(__file__).resolve().parent.parent / "config" / "backend_device.json"),
)
def load_backend_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def save_backend_state(data: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

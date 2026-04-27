import json
from pathlib import Path
from typing import Any


STATE_FILE = Path(__file__).resolve().parent.parent / "config" / "device.json"


def save_state(data: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"configured": False}
    except json.JSONDecodeError:
        return {"configured": False}

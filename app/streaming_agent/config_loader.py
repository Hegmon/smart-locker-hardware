import json
from pathlib import Path

from app.services.backend_state import load_backend_state
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)

BASE_DIR = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "config"
BACKEND_CONFIG_FILE = CONFIG_FILE / "backend_device.json"


def load_backend_device_config():
    if not BACKEND_CONFIG_FILE.exists():
        raise FileNotFoundError(f"Backend config file not found: {BACKEND_CONFIG_FILE}")

    with open(BACKEND_CONFIG_FILE, "r", encoding="utf-8") as file:
        config = json.load(file)

    return config


def get_device_id():
    config = load_backend_state()
    if not config:
        config = load_backend_device_config()

    device_id = str(config.get("device_id") or "").strip() or str(config.get("device_uuid") or "").strip()
    if not device_id:
        raise ValueError("Device ID not found in backend state or backend config")

    logger.info("Loaded device ID %s", device_id)
    return device_id

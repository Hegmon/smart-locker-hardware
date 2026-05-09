import json 
from pathlib import Path

BASE_DIR=Path(__file__).parent.parent
print("Base directory:", BASE_DIR)
CONFIG_FILE=BASE_DIR / "config"
print("Config directory:", CONFIG_FILE)

BACKEND_CONFIG_FILE=CONFIG_FILE / "backend_config.json"
print("Backend config file path:", BACKEND_CONFIG_FILE)

def load_backend_device_config():
    if not BACKEND_CONFIG_FILE.exists():
        raise FileNotFoundError(f"Backend config file not found:{BACKEND_CONFIG_FILE}")
    with open(BACKEND_CONFIG_FILE,"r") as file:
        config=json.load(file)

    return config
def get_device_id():
    config=load_backend_device_config()
    device_id=config.get("device_id")
    if not device_id:
        raise ValueError("Device ID not found in backend config")
    print("Loaded device ID:",device_id)
    return device_id

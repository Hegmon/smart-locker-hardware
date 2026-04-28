def command_topic(device_id: str):
    return f"devices/{device_id}/command"

def scan_topic(device_id: str):
    return f"devices/{device_id}/wifi/scan"

def state_topic(device_id: str):
    return f"devices/{device_id}/wifi/state"

def result_topic(device_id: str):
    return f"devices/{device_id}/command/result"
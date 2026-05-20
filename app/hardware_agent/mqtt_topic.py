def request(device_id:str,service:str):
    return f"devices/{device_id}/services/{service}/request"

def response(device_id:str,service:str):
    return f"devices/{device_id}/services/{service}/response"

def event(device_id:str,event:str):
    if event in {"wifi", "state", "scan"}:
        return f"devices/{device_id}/wifi"
    if event == "stream":
        return f"devices/{device_id}/stream/status"
    if event == "logs":
        return f"devices/{device_id}/logs"
    return f"devices/{device_id}/{event}"

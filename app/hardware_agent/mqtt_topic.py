def request(device_id:str,service:str):
    return f"devices/{device_id}/services/{service}/request"

def response(device_id:str,service:str):
    return f"devices/{device_id}/services/{service}/response"

def event(device_id:str,event:str):
    return f"devices/{device_id}/events/{event}"
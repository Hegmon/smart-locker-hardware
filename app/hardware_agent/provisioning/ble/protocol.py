from dataclasses import dataclass
from typing import Optional,Dict,Any

@dataclass
class BLERequest:
    action:str
    ssid:Optional[str]=None
    password:Optional[str]=None
class BLEProtocolError(Exception):
    pass
def parse_ble_request(payload:Dict[str,Any])->BLERequest:
    if "action" not in payload:
        raise BLEProtocolError("missing action field")
    
    action=payload["action"]

    if action=="connect_wifi":
        return BLERequest(
            action=action,
            ssid=payload.get("ssid"),
            password=payload.get("password","")
        )
    if action =="scan_wifi":
        return BLERequest(action=action)
    raise BLEProtocolError(f"Unknown action:${action}")
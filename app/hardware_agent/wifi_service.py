from services.registry import service
from app.services.wifi_manager import (
    scan_wifi,
    connect_wifi,
    get_connected_wifi_details,
    scan_hotspot,
    reconnect_saved_wifi,
    start_hotspot
)
@service("wifi.scan")
def wifi_scan():
    networks= scan_wifi()
    
    return {
        "networks":networks,
        "connected":get_connected_wifi_details()
    }

@service("wifi.connect")
def wifi_connect(payload):
    ssid=payload.get("ssid")
    password=payload.get("password")
    return connect_wifi(ssid,password)
    previous=get_connected_wifi_details().get("connected_ssid")
    
    try:
        return connect_wifi(ssid,password)
    except Exception as e:
        if previous:
            try:
                reconnect_saved_wifi(previous)
                return {
                    "status":"fallback_reconnected",
                    "ssid":previous,
                     "error":str(e)
                }
            except Exception as e:
                return {
                    "status":"fallback_failed",
                    "ssid":previous,
                    "error":str(e)
                }
            hotspot=start_hotspot()
            
            return {
                "status":"hotspot_mode",
                "hotspot":hotspot,
                "error":str(e)
            }
from enum import Enum 
class ProvisioningState(str,Enum):
    WIFI_ONLINE="wifi_online"
    WIFI_RECONNECTING="wifi_reconnecting"
    WIFI_PROVISIONING="wifi_provisioning"
    BLE_PROVISIONING = "ble_provisioning"
    WIFI_FALLBACK="wifi_fallback"
    IDLE="idle"
    
class DeviceStateManager:
     """
    Prevents conflicts between:
    WiFi, BLE, MQTT, Hotspot
    """
     def __init__(self):
        self.state=ProvisioningState.IDLE
     def set(self,state:ProvisioningState):
        print(f"[STATE] {self.state} -> {state}")
        self.state=state
     def is_ble_active(self)->bool:
         return self.state==ProvisioningState.BLE_PROVISIONING
     
     def is_online(self)->bool:
         return self.state==ProvisioningState.WIFI_ONLINE
    
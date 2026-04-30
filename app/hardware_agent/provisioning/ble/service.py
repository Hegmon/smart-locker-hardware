from app.hardware_agent.provisioning.ble.characteristic import (
    CommandCharacteristic,
    ResponseCharacteristic,
)

SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"


class SmartLockerService:
    def __init__(self, bus, handler):
        self.path = "/org/bluez/example/service0"
        self.bus = bus

        self.command_char = CommandCharacteristic(bus, self, handler)
        self.response_char = ResponseCharacteristic(bus, self)

    def get_properties(self):
        return {
            "org.bluez.GattService1": {
                "UUID": SERVICE_UUID,
                "Primary": True,
                "Characteristics": [
                    self.command_char.path,
                    self.response_char.path,
                ],
            }
        }
import json
import dbus.service

CHAR_COMMAND_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
CHAR_RESPONSE_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"


class CommandCharacteristic(dbus.service.Object):
    def __init__(self, bus, service, handler):
        self.path = service.path + "/char0"
        self.bus = bus
        self.handler = handler
        self.response_char = None

        super().__init__(bus, self.path)

    def get_properties(self):
        return {
            "org.bluez.GattCharacteristic1": {
                "UUID": CHAR_COMMAND_UUID,
                "Flags": ["write"],
            }
        }

    @dbus.service.method("org.bluez.GattCharacteristic1",
                         in_signature="aya{sv}")
    def WriteValue(self, value, options):
        try:
            data = bytes(value).decode()
            payload = json.loads(data)

            print(f"[BLE] RX: {payload}")

            response = self.handler.handle(payload)

            if self.response_char:
                self.response_char.notify(response)

        except Exception as e:
            print(f"[BLE ERROR] {e}")


class ResponseCharacteristic(dbus.service.Object):
    def __init__(self, bus, service):
        self.path = service.path + "/char1"
        self.bus = bus
        self.value = b""

        super().__init__(bus, self.path)

    def get_properties(self):
        return {
            "org.bluez.GattCharacteristic1": {
                "UUID": CHAR_RESPONSE_UUID,
                "Flags": ["notify", "read"],
            }
        }

    def notify(self, data):
        self.value = json.dumps(data).encode()
        print(f"[BLE TX] {data}")

    @dbus.service.method("org.bluez.GattCharacteristic1",
                         in_signature="a{sv}",
                         out_signature="ay")
    def ReadValue(self, options):
        return self.value
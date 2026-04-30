from __future__ import annotations
import dbus
import dbus.service
BLUEZ_SERVICE_NAME = "org.bluez"
LE_ADVERTISEMENT_IFACE = "org.bluez.LEAdvertisement1"


class Advertisement(dbus.service.Object):
    PATH_BASE = "/org/bluez/example/advertisement"

    def __init__(self, bus, index: int, service_uuid: str, device_name: str):
        self.path = self.PATH_BASE + str(index)
        self.bus = bus

        self.service_uuids = [service_uuid]
        self.local_name = device_name
        self.include_tx_power = True

        super().__init__(bus, self.path)

    def get_properties(self):
        return {
            LE_ADVERTISEMENT_IFACE: {
                "Type": "peripheral",
                "ServiceUUIDs": dbus.Array(self.service_uuids, signature="s"),
                "LocalName": dbus.String(self.local_name),
                "IncludeTxPower": dbus.Boolean(self.include_tx_power),
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ss",
                         out_signature="v")
    def Get(self, interface, prop):
        return self.get_properties()[interface][prop]

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="s",
                         out_signature="a{sv}")
    def GetAll(self, interface):
        return self.get_properties()[interface]

    @dbus.service.method(LE_ADVERTISEMENT_IFACE,
                         in_signature="",
                         out_signature="")
    def Release(self):
        print("[BLE] Advertisement released")
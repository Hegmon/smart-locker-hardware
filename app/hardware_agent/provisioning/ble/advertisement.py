from __future__ import annotations
import dbus
import dbus.service

LE_ADVERTISEMENT_IFACE = "org.bluez.LEAdvertisement1"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"


class Advertisement(dbus.service.Object):
    PATH_BASE = "/org/bluez/smartlocker/advertisement"

    def __init__(
        self,
        bus,
        index: int,
        service_uuid: str,
        device_name: str,
        path_base: str | None = None,
    ):
        self.path = (path_base or self.PATH_BASE) + str(index)
        self.bus = bus

        self.service_uuids = [service_uuid]
        self.local_name = device_name
        self.includes = ["tx-power"]
        self.appearance = 0

        super().__init__(bus, self.path)

    def get_properties(self):
        return {
            LE_ADVERTISEMENT_IFACE: {
                "Type": dbus.String("peripheral"),
                "ServiceUUIDs": dbus.Array(
                    [dbus.String(uuid) for uuid in self.service_uuids],
                    signature="s",
                ),
                "LocalName": dbus.String(self.local_name),
                "Discoverable": dbus.Boolean(True),
                "Includes": dbus.Array(
                    [dbus.String(include) for include in self.includes],
                    signature="s",
                ),
                "Appearance": dbus.UInt16(self.appearance),
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, prop):
        return self.get_properties()[interface][prop]

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != LE_ADVERTISEMENT_IFACE:
            raise dbus.exceptions.DBusException(
                "org.freedesktop.DBus.Error.InvalidArgs",
                "Invalid interface requested",
            )
        return self.get_properties()[interface]

    @dbus.service.method(LE_ADVERTISEMENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        pass

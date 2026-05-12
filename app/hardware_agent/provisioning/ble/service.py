from __future__ import annotations

import dbus
import dbus.service

from app.hardware_agent.provisioning.ble.characteristic import (
    CommandCharacteristic,
    ResponseCharacteristic,
)

GATT_SERVICE_IFACE = "org.bluez.GattService1"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"
SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"


class SmartLockerService(dbus.service.Object):
    def __init__(self, bus, handler):
        self.path = "/org/bluez/example/service0"
        self.bus = bus
        super().__init__(bus, self.path)

        self.command_char = CommandCharacteristic(bus, self, handler)
        self.response_char = ResponseCharacteristic(bus, self)

    def get_properties(self):
        return {
            GATT_SERVICE_IFACE: {
                "UUID": dbus.String(SERVICE_UUID),
                "Primary": dbus.Boolean(True),
                "Characteristics": dbus.Array(
                    [
                        dbus.ObjectPath(self.command_char.path),
                        dbus.ObjectPath(self.response_char.path),
                    ],
                    signature="o",
                ),
            }
        }

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != GATT_SERVICE_IFACE:
            raise dbus.exceptions.DBusException(
                "org.freedesktop.DBus.Error.InvalidArgs",
                "Invalid interface requested",
            )
        return self.get_properties()[GATT_SERVICE_IFACE]

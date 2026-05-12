from __future__ import annotations

import json

import dbus
import dbus.service

from app.utils.logger import get_logger


logger = get_logger(__name__)

GATT_CHARACTERISTIC_IFACE = "org.bluez.GattCharacteristic1"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"
CHAR_COMMAND_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
CHAR_RESPONSE_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"


class CommandCharacteristic(dbus.service.Object):
    def __init__(self, bus, service, handler):
        self.path = service.path + "/char0"
        self.bus = bus
        self.service = service
        self.handler = handler
        self.response_char = None

        super().__init__(bus, self.path)

    def get_properties(self):
        return {
            GATT_CHARACTERISTIC_IFACE: {
                "Service": dbus.ObjectPath(self.service.path),
                "UUID": dbus.String(CHAR_COMMAND_UUID),
                "Flags": dbus.Array(["write"], signature="s"),
            }
        }

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != GATT_CHARACTERISTIC_IFACE:
            raise dbus.exceptions.DBusException(
                "org.freedesktop.DBus.Error.InvalidArgs",
                "Invalid interface requested",
            )
        return self.get_properties()[GATT_CHARACTERISTIC_IFACE]

    @dbus.service.method(GATT_CHARACTERISTIC_IFACE, in_signature="aya{sv}", out_signature="")
    def WriteValue(self, value, options):
        try:
            data = bytes(value).decode()
            payload = json.loads(data)
            logger.info("BLE RX payload: %s", payload)

            response = self.handler.handle(payload)

            if self.response_char:
                self.response_char.notify(response)

        except Exception as exc:
            logger.exception("BLE command write failed: %s", exc)


class ResponseCharacteristic(dbus.service.Object):
    def __init__(self, bus, service):
        self.path = service.path + "/char1"
        self.bus = bus
        self.service = service
        self.value = b""
        self.notifying = False

        super().__init__(bus, self.path)

    def get_properties(self):
        return {
            GATT_CHARACTERISTIC_IFACE: {
                "Service": dbus.ObjectPath(self.service.path),
                "UUID": dbus.String(CHAR_RESPONSE_UUID),
                "Flags": dbus.Array(["notify", "read"], signature="s"),
                "Notifying": dbus.Boolean(self.notifying),
            }
        }

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != GATT_CHARACTERISTIC_IFACE:
            raise dbus.exceptions.DBusException(
                "org.freedesktop.DBus.Error.InvalidArgs",
                "Invalid interface requested",
            )
        return self.get_properties()[GATT_CHARACTERISTIC_IFACE]

    def notify(self, data):
        self.value = json.dumps(data).encode()
        logger.info("BLE TX payload: %s", data)

    @dbus.service.method(GATT_CHARACTERISTIC_IFACE, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        return dbus.ByteArray(self.value)

    @dbus.service.method(GATT_CHARACTERISTIC_IFACE, in_signature="", out_signature="")
    def StartNotify(self):
        self.notifying = True

    @dbus.service.method(GATT_CHARACTERISTIC_IFACE, in_signature="", out_signature="")
    def StopNotify(self):
        self.notifying = False

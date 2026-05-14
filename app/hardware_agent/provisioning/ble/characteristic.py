from __future__ import annotations

import ast
import json
import time

import dbus
import dbus.service

from app.utils.logger import get_logger


logger = get_logger(__name__)

GATT_CHARACTERISTIC_IFACE = "org.bluez.GattCharacteristic1"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"
CHAR_COMMAND_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
CHAR_RESPONSE_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
BLE_RESPONSE_CHUNK_SIZE_BYTES = 180
BLE_RESPONSE_CHUNK_DELAY_SECONDS = 0.03


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
                "Flags": dbus.Array(["write", "write-without-response"], signature="s"),
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
        response: dict[str, object]
        try:
            payload = self._decode_payload(bytes(value))
            logger.info("BLE command received: %s", payload.get("action", "unknown"))
            logger.info("BLE RX payload: %s", _redact_payload(payload))
            response = self.handler.handle(payload)
        except Exception as exc:
            logger.exception("BLE command write failed: %s", exc)
            response = {
                "status": "failed",
                "error": str(exc),
                "hint": 'Use JSON like {"action":"scan_wifi"} or {"action":"connect_wifi","ssid":"MyWiFi","password":"********"}',
            }

        if self.response_char:
            self.response_char.notify(response)

    @staticmethod
    def _decode_payload(raw_value: bytes) -> dict[str, object]:
        text = raw_value.decode("utf-8", errors="ignore").replace("\x00", "").strip()
        if not text:
            raise ValueError("empty BLE payload")

        if text in {"scan_wifi", "wifi_scan", "scan"}:
            return {"action": "scan_wifi"}

        if text in {"connect_wifi", "wifi_connect", "connect"}:
            raise ValueError("connect_wifi requires ssid and password fields")

        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            try:
                decoded = ast.literal_eval(text)
            except Exception as exc:
                raise ValueError(f"invalid BLE payload: {text}") from exc

        if not isinstance(decoded, dict):
            raise ValueError("BLE payload must decode to an object/dictionary")
        return decoded


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
                "Value": dbus.Array([dbus.Byte(byte) for byte in self.value], signature="y"),
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
        self.value = json.dumps(data, separators=(",", ":")).encode("utf-8")
        logger.info("BLE TX payload: %s", _redact_payload(data))
        chunks = list(_chunk_bytes(self.value, BLE_RESPONSE_CHUNK_SIZE_BYTES))
        logger.info(
            "BLE response byte length=%d chunks=%d chunk_size=%d",
            len(self.value),
            len(chunks),
            BLE_RESPONSE_CHUNK_SIZE_BYTES,
        )

        if self.notifying:
            for index, chunk in enumerate(chunks, start=1):
                logger.info(
                    "BLE response chunk %d/%d byte_length=%d",
                    index,
                    len(chunks),
                    len(chunk),
                )
                try:
                    self.PropertiesChanged(
                        GATT_CHARACTERISTIC_IFACE,
                        {"Value": _dbus_byte_array(chunk)},
                        [],
                    )
                    logger.info("BLE response chunk %d/%d notify success", index, len(chunks))
                except Exception:
                    logger.exception("BLE response chunk %d/%d notify failed", index, len(chunks))
                if index < len(chunks):
                    time.sleep(BLE_RESPONSE_CHUNK_DELAY_SECONDS)
        else:
            logger.info("BLE response stored for read because notifications are not enabled")

    @dbus.service.method(GATT_CHARACTERISTIC_IFACE, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        logger.info("BLE response characteristic read")
        return dbus.ByteArray(self.value)

    @dbus.service.method(GATT_CHARACTERISTIC_IFACE, in_signature="", out_signature="")
    def StartNotify(self):
        self.notifying = True
        logger.info("BLE notifications enabled")
        self.PropertiesChanged(
            GATT_CHARACTERISTIC_IFACE,
            {"Notifying": dbus.Boolean(True)},
            [],
        )

    @dbus.service.method(GATT_CHARACTERISTIC_IFACE, in_signature="", out_signature="")
    def StopNotify(self):
        self.notifying = False
        logger.info("BLE notifications disabled")
        self.PropertiesChanged(
            GATT_CHARACTERISTIC_IFACE,
            {"Notifying": dbus.Boolean(False)},
            [],
        )

    @dbus.service.signal(DBUS_PROP_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass


def _redact_payload(payload):
    if isinstance(payload, dict):
        redacted = {}
        for key, value in payload.items():
            if str(key).lower() in {"password", "psk", "secret"}:
                redacted[key] = "********"
            else:
                redacted[key] = _redact_payload(value)
        return redacted
    if isinstance(payload, list):
        return [_redact_payload(item) for item in payload]
    return payload


def _chunk_bytes(data: bytes, chunk_size: int):
    for offset in range(0, len(data), chunk_size):
        yield data[offset : offset + chunk_size]


def _dbus_byte_array(data: bytes):
    return dbus.Array([dbus.Byte(byte) for byte in data], signature="y")

from __future__ import annotations

import dbus
import dbus.service
import dbus.mainloop.glib

from gi.repository import GLib

from app.hardware_agent.provisioning.ble.handler import BLEHandler
from app.hardware_agent.provisioning.ble.utils import get_device_name
from app.hardware_agent.provisioning.ble.service import SmartLockerService, SERVICE_UUID
from app.hardware_agent.provisioning.ble.advertisement import Advertisement


BLUEZ_SERVICE_NAME = "org.bluez"
ADAPTER_PATH = "/org/bluez/hci0"


# =========================================================
# BLE APPLICATION (REQUIRED BY BLUEZ)
# =========================================================
class Application(dbus.service.Object):
    def __init__(self, bus):
        self.path = "/"
        self.services = []
        super().__init__(bus, self.path)

    def add_service(self, service):
        self.services.append(service)

    @dbus.service.method(
        "org.freedesktop.DBus.ObjectManager",
        out_signature="a{oa{sa{sv}}}"
    )
    def GetManagedObjects(self):
        response = {}

        for service in self.services:
            response[service.path] = service.get_properties()

            # include characteristics
            for char in [service.command_char, service.response_char]:
                response[char.path] = char.get_properties()

        return response


# =========================================================
# BLE SERVER
# =========================================================
class BLEServer:
    def __init__(self, interface: str):
        self.interface = interface
        self.handler = BLEHandler(interface)

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

        self.bus = dbus.SystemBus()
        self.loop = GLib.MainLoop()

        self.advertisement = None

    # -----------------------------------------------------
    # START
    # -----------------------------------------------------
    def start(self):
        print("[BLE] Initializing BLE server...")

        adapter = self._get_adapter()

        device_name = get_device_name()

        # Adapter setup
        adapter.Set("org.bluez.Adapter1", "Alias", device_name)
        adapter.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(1))
        adapter.Set("org.bluez.Adapter1", "Discoverable", dbus.Boolean(1))
        adapter.Set("org.bluez.Adapter1", "Pairable", dbus.Boolean(1))

        print(f"[BLE] Device Name: {device_name}")

        # ---------------- APPLICATION ----------------
        app = Application(self.bus)

        service = SmartLockerService(self.bus, self.handler)
        app.add_service(service)

        # Link response channel
        service.command_char.response_char = service.response_char

        # ---------------- GATT REGISTER ----------------
        service_manager = dbus.Interface(
            self.bus.get_object(BLUEZ_SERVICE_NAME, ADAPTER_PATH),
            "org.bluez.GattManager1"
        )

        service_manager.RegisterApplication(
            app.path,
            {},
            reply_handler=lambda: print("[BLE] GATT registered"),
            error_handler=lambda e: print(f"[BLE GATT ERROR] {e}")
        )

        # ---------------- ADVERTISEMENT ----------------
        ad_manager = dbus.Interface(
            self.bus.get_object(BLUEZ_SERVICE_NAME, ADAPTER_PATH),
            "org.bluez.LEAdvertisingManager1"
        )

        self.advertisement = Advertisement(
            self.bus,
            index=0,
            service_uuid=SERVICE_UUID,
            device_name=device_name
        )

        ad_manager.RegisterAdvertisement(
            self.advertisement.get_path(),
            {},
            reply_handler=lambda: print("[BLE] Advertisement registered"),
            error_handler=lambda e: print(f"[BLE ADV ERROR] {e}")
        )

        print("[BLE] BLE server running...")
        self.loop.run()

    # -----------------------------------------------------
    # STOP (IMPORTANT CLEANUP)
    # -----------------------------------------------------
    def stop(self):
        print("[BLE] Stopping BLE server")

        try:
            if self.advertisement:
                ad_manager = dbus.Interface(
                    self.bus.get_object(BLUEZ_SERVICE_NAME, ADAPTER_PATH),
                    "org.bluez.LEAdvertisingManager1"
                )

                ad_manager.UnregisterAdvertisement(
                    self.advertisement.get_path()
                )

                print("[BLE] Advertisement unregistered")

        except Exception as e:
            print(f"[BLE STOP ERROR] {e}")

        self.loop.quit()

    # -----------------------------------------------------
    # INTERNAL
    # -----------------------------------------------------
    def _get_adapter(self):
        obj = self.bus.get_object(BLUEZ_SERVICE_NAME, ADAPTER_PATH)
        return dbus.Interface(obj, "org.freedesktop.DBus.Properties")
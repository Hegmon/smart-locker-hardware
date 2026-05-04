from __future__ import annotations

import threading
import subprocess
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

from app.hardware_agent.provisioning.ble.advertisement import Advertisement
from app.hardware_agent.provisioning.ble.handler import BLEHandler
from app.hardware_agent.provisioning.ble.service import SERVICE_UUID, SmartLockerService
from app.hardware_agent.provisioning.ble.utils import get_device_name
from app.utils.logger import get_logger


logger = get_logger(__name__)

BLUEZ_SERVICE_NAME = "org.bluez"
ADAPTER_PATH = "/org/bluez/hci0"


class Application(dbus.service.Object):
    def __init__(self, bus):
        self.path = "/"
        self.services = []
        super().__init__(bus, self.path)

    def add_service(self, service):
        self.services.append(service)

    @dbus.service.method("org.freedesktop.DBus.ObjectManager", out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        response = {}
        for service in self.services:
            response[service.path] = service.get_properties()
            for characteristic in [service.command_char, service.response_char]:
                response[characteristic.path] = characteristic.get_properties()
        return response


class BLEServer:
    def __init__(self, interface: str, on_wifi_connected=None):
        self.interface = interface
        self.handler = BLEHandler(interface, on_wifi_connected=on_wifi_connected)

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()

        self.loop: GLib.MainLoop | None = None
        self.advertisement = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.RLock()

    def start_async(self) -> bool:
        with self._lock:
            if self._running or (self._thread and self._thread.is_alive()):
                return False

            self._thread = threading.Thread(target=self.start, daemon=True, name="ble-server")
            self._thread.start()
            return True

    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True

        try:
            logger.info("Starting BLE provisioning server")
            self.loop = GLib.MainLoop()
            adapter = self._get_adapter()
            device_name = get_device_name()

            self._prepare_adapter(adapter, device_name)

            app = Application(self.bus)
            service = SmartLockerService(self.bus, self.handler)
            app.add_service(service)
            service.command_char.response_char = service.response_char

            service_manager = dbus.Interface(
                self.bus.get_object(BLUEZ_SERVICE_NAME, ADAPTER_PATH),
                "org.bluez.GattManager1",
            )
            service_manager.RegisterApplication(
                app.path,
                {},
                reply_handler=lambda: logger.info("BLE GATT registered"),
                error_handler=lambda error: logger.error("BLE GATT register error: %s", error),
            )

            ad_manager = dbus.Interface(
                self.bus.get_object(BLUEZ_SERVICE_NAME, ADAPTER_PATH),
                "org.bluez.LEAdvertisingManager1",
            )
            self.advertisement = Advertisement(
                self.bus,
                index=0,
                service_uuid=SERVICE_UUID,
                device_name=device_name,
            )
            ad_manager.RegisterAdvertisement(
                self.advertisement.get_path(),
                {},
                reply_handler=lambda: logger.info("BLE advertisement registered"),
                error_handler=lambda error: logger.error("BLE advertisement register error: %s", error),
            )

            self.loop.run()

        except Exception:
            logger.exception("BLE server failed")
        finally:
            with self._lock:
                self._running = False
                self.advertisement = None
                self.loop = None
                self._thread = None
            logger.info("BLE provisioning server stopped")

    def stop(self) -> bool:
        with self._lock:
            if not self._running and self.advertisement is None and self.loop is None:
                return False

            advertisement = self.advertisement
            loop = self.loop
            self.advertisement = None

        if advertisement is not None:
            try:
                ad_manager = dbus.Interface(
                    self.bus.get_object(BLUEZ_SERVICE_NAME, ADAPTER_PATH),
                    "org.bluez.LEAdvertisingManager1",
                )
                ad_manager.UnregisterAdvertisement(advertisement.get_path())
            except Exception:
                logger.exception("BLE advertisement stop failed")

        if loop is not None and loop.is_running():
            try:
                loop.quit()
            except Exception:
                logger.exception("BLE loop stop failed")

        return True

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def _get_adapter(self):
        obj = self.bus.get_object(BLUEZ_SERVICE_NAME, ADAPTER_PATH)
        return dbus.Interface(obj, "org.freedesktop.DBus.Properties")

    def _prepare_adapter(self, adapter, device_name: str) -> None:
        self._run_best_effort(["rfkill", "unblock", "bluetooth"])
        self._run_best_effort(["bluetoothctl", "power", "on"])
        self._run_best_effort(["hciconfig", "hci0", "up"])

        self._set_adapter_property(adapter, "Alias", device_name, required=False)
        self._set_adapter_property(adapter, "Powered", dbus.Boolean(1), required=True)
        self._set_adapter_property(adapter, "Pairable", dbus.Boolean(1), required=False)
        self._set_adapter_property(adapter, "Discoverable", dbus.Boolean(1), required=False)
        self._set_adapter_property(adapter, "DiscoverableTimeout", dbus.UInt32(0), required=False)
        self._set_adapter_property(adapter, "PairableTimeout", dbus.UInt32(0), required=False)

    def _set_adapter_property(self, adapter, name: str, value, *, required: bool) -> bool:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                adapter.Set("org.bluez.Adapter1", name, value)
                logger.info("BLE adapter %s set to %s", name, value)
                return True
            except dbus.exceptions.DBusException as exc:
                last_error = exc
                logger.warning(
                    "BLE adapter %s failed on attempt %d/3: %s",
                    name,
                    attempt + 1,
                    exc,
                )
                self._run_best_effort(["rfkill", "unblock", "bluetooth"])
                self._run_best_effort(["bluetoothctl", "power", "on"])
                time.sleep(1)

        if required and last_error is not None:
            raise last_error
        return False

    @staticmethod
    def _run_best_effort(command: list[str]) -> None:
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        except FileNotFoundError:
            logger.debug("BLE helper command not found: %s", command[0])
        except Exception as exc:
            logger.debug("BLE helper command failed (%s): %s", " ".join(command), exc)

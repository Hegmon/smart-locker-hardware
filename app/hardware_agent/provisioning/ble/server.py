from __future__ import annotations

import asyncio
import threading
import subprocess
import time
import uuid

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
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"
BLUEZ_DEVICE_IFACE = "org.bluez.Device1"


class Application(dbus.service.Object):
    def __init__(self, bus, path: str):
        self.path = path
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
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._running = False
        self._bluetooth_enabled = False
        self._advertising_active = False
        self._gatt_registered = False
        self._startup_failed = False
        self._stop_requested = False
        self._app = None
        self._service = None
        self._connected_devices: set[str] = set()
        self._device_signal_registered = False
        self._lock = threading.RLock()

    def start_async(self) -> bool:
        with self._lock:
            if self._running or (self._thread and self._thread.is_alive()):
                return False

            self._thread = threading.Thread(target=self.start, daemon=True, name="ble-server")
            self._thread.start()
            return True

    def start(self):
        asyncio.run(self._run())

    async def _run(self):
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_requested = False
            self._async_loop = asyncio.get_running_loop()
            self._stop_event = asyncio.Event()

        try:
            logger.info("Starting BLE provisioning server")
            self.loop = GLib.MainLoop()
            with self._lock:
                self._startup_failed = False
            adapter = self._get_adapter()
            device_name = get_device_name()

            self._prepare_adapter(adapter, device_name)
            with self._lock:
                self._bluetooth_enabled = True
            self._register_device_signal_receiver()

            if self._should_stop():
                logger.info("BLE startup canceled before GATT registration")
                self._disable_bluetooth()
                return

            run_id = uuid.uuid4().hex[:8]
            app_path = f"/org/bluez/smartlocker/{run_id}"
            app = Application(self.bus, app_path)
            service = SmartLockerService(self.bus, self.handler, app_path=app_path)
            app.add_service(service)
            service.command_char.response_char = service.response_char
            with self._lock:
                self._app = app
                self._service = service

            service_manager = dbus.Interface(
                self.bus.get_object(BLUEZ_SERVICE_NAME, ADAPTER_PATH),
                "org.bluez.GattManager1",
            )
            service_manager.RegisterApplication(
                app.path,
                {},
                reply_handler=lambda: self._on_gatt_registered(device_name),
                error_handler=self._on_gatt_registration_error,
            )

            await self._pump_bluez_events()

        except Exception:
            logger.exception("BLE server failed")
            self._disable_bluetooth()
        finally:
            self._cleanup_dbus_objects()
            with self._lock:
                self._running = False
                self.advertisement = None
                self.loop = None
                self._thread = None
                self._async_loop = None
                self._stop_event = None
                self._advertising_active = False
                self._gatt_registered = False
                self._app = None
                self._service = None
                self._connected_devices.clear()
            logger.info("BLE provisioning server stopped")

    def stop(self) -> bool:
        with self._lock:
            self._stop_requested = True
            if not self._running and self.advertisement is None and self.loop is None:
                self._disable_bluetooth()
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
                with self._lock:
                    self._advertising_active = False
                logger.info("BLE advertising stopped")
            except Exception:
                logger.exception("BLE advertisement stop failed")

        if loop is not None and loop.is_running():
            try:
                loop.quit()
            except Exception:
                logger.exception("BLE loop stop failed")

        async_loop = None
        stop_event = None
        with self._lock:
            async_loop = self._async_loop
            stop_event = self._stop_event
        if async_loop is not None and stop_event is not None:
            try:
                async_loop.call_soon_threadsafe(stop_event.set)
            except Exception:
                logger.exception("BLE asyncio loop stop failed")

        self._disable_bluetooth()

        return True

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def is_bluetooth_enabled(self) -> bool:
        with self._lock:
            return self._bluetooth_enabled

    def is_advertising(self) -> bool:
        with self._lock:
            return self._advertising_active

    def startup_failed(self) -> bool:
        with self._lock:
            return self._startup_failed

    def connected_devices(self) -> list[str]:
        with self._lock:
            return sorted(self._connected_devices)

    async def _pump_bluez_events(self) -> None:
        stop_event = self._stop_event
        context = self.loop.get_context() if self.loop is not None else GLib.MainContext.default()
        while stop_event is not None and not stop_event.is_set():
            while context.pending():
                context.iteration(False)
            await asyncio.sleep(0.05)

    def _get_adapter(self):
        obj = self.bus.get_object(BLUEZ_SERVICE_NAME, ADAPTER_PATH)
        return dbus.Interface(obj, "org.freedesktop.DBus.Properties")

    def _prepare_adapter(self, adapter, device_name: str) -> None:
        self._run_best_effort(["rfkill", "unblock", "bluetooth"])
        self._run_best_effort(["bluetoothctl", "power", "on"])
        self._run_best_effort(["hciconfig", "hci0", "up"])
        logger.info("Bluetooth enabled")

        self._set_adapter_property(adapter, "Alias", device_name, required=False)
        self._set_adapter_property(adapter, "Powered", dbus.Boolean(1), required=True)
        self._set_adapter_property(adapter, "Pairable", dbus.Boolean(0), required=False)
        self._set_adapter_property(adapter, "Discoverable", dbus.Boolean(0), required=False)
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

    def _disable_bluetooth(self) -> None:
        try:
            adapter = self._get_adapter()
            self._set_adapter_property(adapter, "Discoverable", dbus.Boolean(0), required=False)
            self._set_adapter_property(adapter, "Pairable", dbus.Boolean(0), required=False)
            self._set_adapter_property(adapter, "Powered", dbus.Boolean(0), required=False)
        except Exception:
            logger.exception("Bluetooth adapter power down failed")
        finally:
            self._run_best_effort(["bluetoothctl", "power", "off"])
            self._run_best_effort(["hciconfig", "hci0", "down"])
            with self._lock:
                self._bluetooth_enabled = False
                self._advertising_active = False
            logger.info("Bluetooth disabled")

    def _on_advertisement_registered(self) -> None:
        with self._lock:
            self._advertising_active = True
        logger.info("BLE advertising started")

    def _on_gatt_registered(self, device_name: str) -> None:
        if self._should_stop():
            logger.info("BLE GATT registered after stop request; shutting down")
            self._shutdown_failed_ble()
            return

        with self._lock:
            self._gatt_registered = True
        logger.info("BLE GATT registered")

        try:
            ad_manager = dbus.Interface(
                self.bus.get_object(BLUEZ_SERVICE_NAME, ADAPTER_PATH),
                "org.bluez.LEAdvertisingManager1",
            )
            self.advertisement = Advertisement(
                self.bus,
                index=0,
                service_uuid=SERVICE_UUID,
                device_name=device_name,
                path_base=f"{self._app.path}/advertisement" if self._app is not None else None,
            )
            ad_manager.RegisterAdvertisement(
                self.advertisement.get_path(),
                {},
                reply_handler=self._on_advertisement_ready,
                error_handler=self._on_advertisement_error,
            )
        except Exception:
            logger.exception("BLE advertisement registration setup failed")
            self._shutdown_failed_ble()

    def _on_advertisement_ready(self) -> None:
        if self._should_stop():
            logger.info("BLE advertisement ready after stop request; shutting down")
            self._shutdown_failed_ble()
            return

        try:
            adapter = self._get_adapter()
            self._set_adapter_property(adapter, "Pairable", dbus.Boolean(0), required=False)
            self._set_adapter_property(adapter, "Discoverable", dbus.Boolean(0), required=False)
            self._on_advertisement_registered()
        except Exception:
            logger.exception("BLE advertisement activation failed")
            self._shutdown_failed_ble()

    def _on_gatt_registration_error(self, error) -> None:
        logger.error("BLE GATT register error: %s", error)
        self._shutdown_failed_ble()

    def _on_advertisement_error(self, error) -> None:
        logger.error("BLE advertisement register error: %s", error)
        self._shutdown_failed_ble()

    def _shutdown_failed_ble(self) -> None:
        with self._lock:
            self._startup_failed = True
        self._disable_bluetooth()
        loop = self.loop
        if loop is not None and loop.is_running():
            try:
                loop.quit()
            except Exception:
                logger.exception("BLE loop stop failed during error shutdown")
        async_loop = None
        stop_event = None
        with self._lock:
            async_loop = self._async_loop
            stop_event = self._stop_event
        if async_loop is not None and stop_event is not None:
            try:
                async_loop.call_soon_threadsafe(stop_event.set)
            except Exception:
                logger.exception("BLE asyncio loop stop failed during error shutdown")

    def _should_stop(self) -> bool:
        with self._lock:
            return self._stop_requested

    def _cleanup_dbus_objects(self) -> None:
        if self._device_signal_registered:
            try:
                self.bus.remove_signal_receiver(
                    self._on_device_properties_changed,
                    dbus_interface=DBUS_PROP_IFACE,
                    signal_name="PropertiesChanged",
                )
            except Exception:
                logger.debug("BLE device signal cleanup skipped", exc_info=True)
            finally:
                self._device_signal_registered = False

        objects = []
        with self._lock:
            service = self._service
            app = self._app
        if service is not None:
            objects.extend([service.command_char, service.response_char, service])
        if app is not None:
            objects.append(app)

        for obj in objects:
            try:
                obj.remove_from_connection()
            except Exception:
                logger.debug("BLE dbus object cleanup skipped for %s", getattr(obj, "path", obj), exc_info=True)

    @staticmethod
    def _run_best_effort(command: list[str]) -> None:
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        except FileNotFoundError:
            logger.debug("BLE helper command not found: %s", command[0])
        except Exception as exc:
            logger.debug("BLE helper command failed (%s): %s", " ".join(command), exc)

    def _register_device_signal_receiver(self) -> None:
        if self._device_signal_registered:
            return
        self.bus.add_signal_receiver(
            self._on_device_properties_changed,
            dbus_interface=DBUS_PROP_IFACE,
            signal_name="PropertiesChanged",
            path_keyword="path",
        )
        self._device_signal_registered = True

    def _on_device_properties_changed(self, interface, changed, invalidated, path=None) -> None:
        if interface != BLUEZ_DEVICE_IFACE or "Connected" not in changed:
            return

        connected = bool(changed["Connected"])
        device_path = str(path or "")
        with self._lock:
            if connected:
                self._connected_devices.add(device_path)
            else:
                self._connected_devices.discard(device_path)

        logger.info(
            "BLE central %s: %s",
            "connected" if connected else "disconnected",
            device_path or "unknown-device",
        )

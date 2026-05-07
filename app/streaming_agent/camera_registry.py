"""
Camera Registry Module
Manages the collection of detected cameras and handles dynamic changes.
"""

from __future__ import annotations
import logging
import threading
import time
from typing import Dict, List, Optional, Callable, Set
from dataclasses import dataclass, field

from .camera_classifier import CameraClassifier, CameraClassification
from .camera_capabilities import CameraCapabilitiesDetector, CameraCapabilities

logger = logging.getLogger(__name__)


@dataclass
class CameraDevice:
    """Represents a registered camera device"""
    device_path: str
    classification: CameraClassification
    capabilities: CameraCapabilities
    last_seen: float = 0.0
    is_active: bool = True
    stream_id: Optional[str] = None  # Associated stream ID if streaming


class CameraRegistry:
    """Registry of all detected camera devices with hotplug support"""

    SCAN_INTERVAL = 5.0  # seconds between scans
    DEVICE_TIMEOUT = 30.0  # seconds before considering device disconnected

    def __init__(self):
        self.devices: Dict[str, CameraDevice] = {}
        self.classifier = CameraClassifier()
        self.capabilities_detector = CameraCapabilitiesDetector()

        self._lock = threading.RLock()
        self._running = False
        self._scan_thread: Optional[threading.Thread] = None
        self._callbacks: List[Callable[[str, CameraDevice, str], None]] = []

        # Track known device paths to detect changes
        self._known_paths: Set[str] = set()

    def start_monitoring(self) -> None:
        """Start background monitoring for device changes"""
        with self._lock:
            if self._running:
                return

            self._running = True
            self._scan_thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
                name="camera-monitor"
            )
            self._scan_thread.start()
            logger.info("Camera registry monitoring started")

    def stop_monitoring(self) -> None:
        """Stop background monitoring"""
        with self._lock:
            self._running = False

        if self._scan_thread:
            self._scan_thread.join(timeout=5)
            logger.info("Camera registry monitoring stopped")

    def add_change_callback(self, callback: Callable[[str, CameraDevice, str], None]) -> None:
        """
        Add callback for device changes.
        Callback signature: (action, device, reason)
        Actions: "added", "removed", "updated"
        """
        with self._lock:
            self._callbacks.append(callback)

    def scan_devices(self) -> Dict[str, CameraDevice]:
        """
        Perform a full device scan and update registry.
        Returns current device snapshot.
        """
        from .camera_detector import CameraDetector
        detector = CameraDetector()

        # Get raw camera list
        raw_cameras = detector.detect_cameras()

        # Convert to our device format
        current_devices = {}
        current_paths = set()

        for cam_info in raw_cameras:
            device_path = cam_info.device_path

            # Get detailed capabilities
            capabilities = self.capabilities_detector.detect_capabilities(device_path)

            # Classify device
            classification = self.classifier.classify_device(
                device_path=device_path,
                device_name=cam_info.name,
                bus_info=capabilities.bus_info,
                capabilities=capabilities.capabilities
            )

            # Skip invalid or non-camera devices
            if not capabilities.is_valid or classification.backend == "none":
                continue

            device = CameraDevice(
                device_path=device_path,
                classification=classification,
                capabilities=capabilities,
                last_seen=time.time(),
                is_active=True
            )

            current_devices[device_path] = device
            current_paths.add(device_path)

        # Update registry with changes
        self._update_registry(current_devices, current_paths)

        return dict(self.devices)

    def _update_registry(self, current_devices: Dict[str, CameraDevice],
                        current_paths: Set[str]) -> None:
        """Update registry and notify callbacks of changes"""
        with self._lock:
            # Check for removed devices
            removed_paths = self._known_paths - current_paths
            for path in removed_paths:
                if path in self.devices:
                    device = self.devices[path]
                    device.is_active = False
                    self._notify_callbacks("removed", device, "device_disconnected")

            # Check for new or updated devices
            for path, device in current_devices.items():
                if path not in self.devices:
                    # New device
                    self.devices[path] = device
                    self._notify_callbacks("added", device, "device_detected")
                else:
                    # Existing device - check if changed
                    existing = self.devices[path]
                    if (existing.classification.backend != device.classification.backend or
                        existing.capabilities.supported_formats != device.capabilities.supported_formats):
                        self.devices[path] = device
                        self._notify_callbacks("updated", device, "capabilities_changed")
                    else:
                        # Just update last seen
                        existing.last_seen = device.last_seen

            # Update known paths
            self._known_paths = current_paths.copy()

    def _monitor_loop(self) -> None:
        """Background monitoring loop"""
        while self._running:
            try:
                self.scan_devices()
                self._cleanup_stale_devices()
            except Exception as e:
                logger.exception(f"Error in camera monitoring loop: {e}")

            time.sleep(self.SCAN_INTERVAL)

    def _cleanup_stale_devices(self) -> None:
        """Remove devices that haven't been seen for too long"""
        cutoff_time = time.time() - self.DEVICE_TIMEOUT

        with self._lock:
            to_remove = []
            for path, device in self.devices.items():
                if device.last_seen < cutoff_time and not device.is_active:
                    to_remove.append(path)

            for path in to_remove:
                logger.info(f"Removing stale device: {path}")
                del self.devices[path]
                self._known_paths.discard(path)

    def _notify_callbacks(self, action: str, device: CameraDevice, reason: str) -> None:
        """Notify all registered callbacks of device changes"""
        for callback in self._callbacks:
            try:
                callback(action, device, reason)
            except Exception as e:
                logger.exception(f"Error in device change callback: {e}")

    def get_active_devices(self) -> List[CameraDevice]:
        """Get list of currently active devices"""
        with self._lock:
            return [d for d in self.devices.values() if d.is_active]

    def get_device_by_path(self, device_path: str) -> Optional[CameraDevice]:
        """Get device by path"""
        with self._lock:
            return self.devices.get(device_path)

    def get_devices_by_backend(self, backend: str) -> List[CameraDevice]:
        """Get devices by backend type"""
        with self._lock:
            return [d for d in self.devices.values()
                    if d.classification.backend == backend and d.is_active]

    def get_streaming_devices(self):
        """Get devices that are currently streaming"""
        with self._lock:
            result = []
            for device in self.devices.values():
                if device.stream_id is not None and device.is_active:
                    result.append(device)
            return result</content>
<parameter name="filePath">/home/hassaanqazi/Documents/smart-locker-hardware/app/streaming_agent/camera_registry.py

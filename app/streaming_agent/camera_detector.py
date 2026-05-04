"""
Camera Detection and Classification
Auto-detects internal (CSI/platform) vs external (USB) cameras on Raspberry Pi.

Logic:
- INTERNAL_CAMERA_DEVICE and EXTERNAL_CAMERA_DEVICE env vars override auto-detection
- Uses sysfs to check device bus (platform/PCI vs USB) for accurate classification
- Falls back to positional heuristic if sysfs is unavailable
"""
from __future__ import annotations

import os
from glob import glob
from pathlib import Path
from typing import NamedTuple, Optional

from .device_config import get_optional_config


class CameraInfo(NamedTuple):
    """Information about a detected camera"""
    device_path: str
    camera_type: str  # "internal" or "external"
    index: int  # 0-based index among detected cameras


class CameraDetector:
    """Detects and classifies cameras for streaming"""
    
    def __init__(self):
        # Explicit overrides from env/config (None if not set)
        self.override_internal = (
            os.getenv("INTERNAL_CAMERA_DEVICE")
            or get_optional_config("INTERNAL_CAMERA_DEVICE")
        )
        self.override_external = (
            os.getenv("EXTERNAL_CAMERA_DEVICE")
            or get_optional_config("EXTERNAL_CAMERA_DEVICE")
        )
    
    def _get_device_bus(self, device_path: str) -> Optional[str]:
        """
        Check sysfs to determine the bus type of a video device.
        Returns "platform" for internal/CSI, "usb" for external USB, or None if unknown.
        """
        try:
            # /sys/class/video4linux/videoX -> /sys/devices/.../videoX
            video_dir = Path(f"/sys/class/video4linux/{Path(device_path).name}")
            if not video_dir.exists():
                return None
            
            # Follow the 'device' symlink to get the physical device
            device_link = video_dir / "device"
            if not device_link.exists() or not device_link.is_symlink():
                return None
            
            # Resolve the symlink target
            target = os.readlink(device_link)
            
            # Check if the device path indicates USB or platform/PCI
            if "/usb" in target or "/usb" in str(Path(target).parents):
                return "usb"
            elif "/platform" in target:
                return "platform"
            # Some PCI devices may not have explicit usb in path but under pci
            elif "/pci" in target:
                return "pci"  # could be capture card etc
            else:
                return None
        except (OSError, ValueError):
            return None
    
    def _get_device_name(self, device_path: str) -> Optional[str]:
        """Read the video device's friendly name from sysfs."""
        try:
            name_file = Path(f"/sys/class/video4linux/{Path(device_path).name}/name")
            if name_file.exists():
                return name_file.read_text().strip()
        except Exception:
            pass
        return None
    
    def detect_cameras(self) -> list[CameraInfo]:
        """
        Detect available video devices and classify them.
        
        Strategy:
        1. Use sysfs to determine bus type (platform=internal, usb=external)
        2. If no platform cameras, use device name heuristics to guess internal among USB
        3. Positional fallback if all else fails (first device = internal)
        4. Apply explicit overrides if configured (forces a device to internal/external)
        """
        devices = sorted(glob("/dev/video*"))
        
        if not devices:
            return []
        
        # Partition devices by bus type using sysfs
        platform_cameras: list[str] = []
        usb_cameras: list[str] = []
        unclassified: list[str] = []
        
        for device in devices:
            bus = self._get_device_bus(device)
            if bus == "platform":
                platform_cameras.append(device)
            elif bus == "usb":
                usb_cameras.append(device)
            else:
                unclassified.append(device)
        
        # Determine internal and external sets
        internal_set = set(platform_cameras)
        external_set: set[str] = set(usb_cameras) | set(unclassified)
        
        # If no internal camera yet, try to infer from USB device name
        if not internal_set and external_set:
            for dev in sorted(external_set):
                name = self._get_device_name(dev)
                if name and any(kw in name.lower() for kw in ["integrated", "built-in", "internal", "onboard"]):
                    internal_set.add(dev)
                    external_set.remove(dev)
                    break
            # If still no internal, fall back to first device
            if not internal_set:
                first = devices[0]
                internal_set.add(first)
                external_set.discard(first)
        
        # Apply explicit overrides (force classification)
        if self.override_internal and os.path.exists(self.override_internal):
            internal_set.add(self.override_internal)
            external_set.discard(self.override_internal)
        if self.override_external and os.path.exists(self.override_external):
            external_set.add(self.override_external)
            internal_set.discard(self.override_external)
        
        # Build CameraInfo list
        result = []
        for idx, device in enumerate(devices):
            cam_type = "internal" if device in internal_set else "external"
            result.append(CameraInfo(
                device_path=device,
                camera_type=cam_type,
                index=idx
            ))
        return result
    
    def get_internal_camera(self) -> Optional[CameraInfo]:
        """Get internal camera if available"""
        for cam in self.detect_cameras():
            if cam.camera_type == "internal":
                return cam
        return None
    
    def get_external_camera(self) -> Optional[CameraInfo]:
        """Get external camera if available"""
        for cam in self.detect_cameras():
            if cam.camera_type == "external":
                return cam
        return None
    
    def get_cameras_for_streaming(self) -> dict[str, CameraInfo]:
        """
        Return dict with keys 'internal' and 'external' if cameras available.
        Missing cameras are simply omitted from the dict.
        """
        result = {}
        internal = self.get_internal_camera()
        if internal:
            result["internal"] = internal
        external = self.get_external_camera()
        if external:
            result["external"] = external
        return result

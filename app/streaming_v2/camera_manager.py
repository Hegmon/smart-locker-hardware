"""
Camera Manager - Core Layer

Discovers cameras and profiles formats without locking devices.
Uses ephemeral probing to prevent "Device busy" errors.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from glob import glob
from pathlib import Path
from typing import List, Optional, Dict, Any

from .models import CameraConfig

logger = logging.getLogger(__name__)


class CameraManager:
    """
    Discovers and profiles cameras without locking devices.
    
    Features:
    - Lists /dev/video* devices
    - Probes formats using v4l2-ctl and ffmpeg
    - NEVER locks devices during probing
    - Caches per-device format + resolution
    - Determines camera type (internal/external)
    """
    
    # Priority order for format selection
    FORMAT_PRIORITY = ["mjpeg", "yuyv422", "h264"]
    
    # Safe resolutions per camera type
    SAFE_RESOLUTIONS = {
        "internal": ["640x480", "1280x720"],
        "external": ["1280x720", "1920x1080", "640x480"],
    }
    
    # Probe timeout (short to avoid blocking)
    PROBE_TIMEOUT = 5
    
    def __init__(self):
        self._cache: Dict[str, CameraConfig] = {}
        self._cache_time: Dict[str, float] = {}
        self._cache_ttl = 300  # 5 minutes
    
    def discover_cameras(self) -> List[CameraConfig]:
        """
        Discover all available cameras.
        
        Returns:
            List of CameraConfig for each detected camera
        """
        devices = self._list_video_devices()
        # Filter out secondary/metadata nodes so we only probe primary capture nodes
        devices = self._filter_primary_nodes(devices)
        logger.info("Discovered %d video device(s): %s", len(devices), devices)
        
        configs = []
        for device in devices:
            config = self._probe_device(device)
            if config:
                configs.append(config)
        
        # Classify internal vs external
        self._classify_cameras(configs)
        
        # Update cache
        for config in configs:
            self._cache[config.device] = config
            self._cache_time[config.device] = time.time()
        
        return configs

    def _filter_primary_nodes(self, devices: List[str]) -> List[str]:
        """
        Filter a list of /dev/video* nodes, keeping only the primary capture
        node for each physical device. This avoids probing metadata/control
        nodes (commonly odd-numbered nodes like /dev/video1, /dev/video3).

        Strategy:
        - Group nodes by physical id (from _get_physical_id)
        - For each physical id, keep the node with the lowest numeric index
        - Return the filtered list sorted by device number
        """
        primary: dict[str, str] = {}

        def dev_num(d: str) -> int:
            import re
            m = re.search(r"(\d+)$", d)
            try:
                return int(m.group(1)) if m else 999
            except Exception:
                return 999

        for dev in devices:
            try:
                phys = self._get_physical_id(dev) or dev
            except Exception:
                phys = dev

            if phys in primary:
                # keep the lower-numbered node as primary
                if dev_num(dev) < dev_num(primary[phys]):
                    primary[phys] = dev
            else:
                primary[phys] = dev

        # Return sorted primary nodes
        filtered = sorted(primary.values(), key=lambda d: dev_num(d))
        return filtered
    
    def get_camera_config(self, device: str) -> Optional[CameraConfig]:
        """
        Get camera config from cache or probe fresh.
        
        Args:
            device: Device path (e.g., /dev/video0)
        
        Returns:
            CameraConfig or None if not found
        """
        # Check cache first
        if device in self._cache:
            age = time.time() - self._cache_time.get(device, 0)
            if age < self._cache_ttl:
                logger.debug("Using cached config for %s (age: %.1fs)", device, age)
                return self._cache[device]
        
        # Probe fresh
        config = self._probe_device(device)
        if config:
            self._cache[device] = config
            self._cache_time[device] = time.time()
        
        return config
    
    def _list_video_devices(self) -> List[str]:
        """
        List available /dev/video* devices.
        
        Returns:
            Sorted list of device paths
        """
        devices: List[str] = []

        # Prefer stable by-id links when available (they encode USB serials)
        try:
            by_id_dir = Path("/dev/v4l/by-id")
            if by_id_dir.exists():
                for entry in sorted(by_id_dir.iterdir()):
                    # Resolve symlink -> /dev/videoX
                    try:
                        if entry.is_symlink():
                            resolved = str(entry.resolve())
                            if resolved and resolved not in devices:
                                devices.append(resolved)
                    except Exception:
                        continue
        except Exception:
            pass

        # Add any remaining /dev/video* nodes not represented in by-id
        for d in glob("/dev/video*"):
            if d not in devices:
                devices.append(d)

        # Sort by device number (video0, video1, ...)
        def _num_key(dev: str) -> int:
            m = re.search(r"(\d+)$", dev)
            try:
                return int(m.group(1)) if m else 999
            except Exception:
                return 999

        devices.sort(key=_num_key)
        return devices
    
    def _probe_device(self, device: str) -> Optional[CameraConfig]:
        """
        Probe a single device for formats and capabilities.
        
        Uses ephemeral probing that never locks the device.
        
        Args:
            device: Device path
        
        Returns:
            CameraConfig or None if probing fails
        """
        logger.info("Probing device: %s", device)

        # Get device name and driver info
        name, driver_info = self._get_device_info(device)

        # If udevadm is available, prefer udev properties for USB detection
        try:
            udev = self._get_udev_properties(device)
        except Exception:
            udev = {}

        # Enforce USB-only detection: skip devices that are not on USB bus
        if udev:
            id_bus = udev.get("ID_BUS")
            if id_bus and id_bus.lower() != "usb":
                logger.info("Skipping non-USB device (udev ID_BUS=%s): %s (%s)", id_bus, device, name)
                return None
        else:
            # Fallback: if no udev info, skip devices that don't appear to be USB via sysfs
            try:
                # If device is not under a USB sysfs path, skip (conservative)
                video_dir = Path(f"/sys/class/video4linux/{Path(device).name}")
                device_link = video_dir / "device"
                is_usb = False
                if device_link.exists() and device_link.is_symlink():
                    target = (video_dir.parent / os.readlink(device_link)).resolve()
                    if "/usb" in str(target):
                        is_usb = True
                if not is_usb:
                    logger.info("Skipping non-USB device (sysfs check): %s (%s)", device, name)
                    return None
            except Exception:
                # If sysfs check fails, continue with probing as a fallback
                pass

        # Skip non-camera devices
        if not self._is_camera_device(name):
            logger.info("Skipping non-camera device: %s (%s)", device, name)
            return None
        
        # Probe formats (ephemeral - never locks device)
        formats = self._probe_formats(device)
        
        if not formats:
            logger.warning("No supported formats detected for %s (%s)", device, name)
            # Still create config with auto-detection fallback
            formats = []
        
        # Select best format
        selected_format = self._select_format(formats)
        
        # Determine safe resolutions
        safe_resolutions = self._determine_resolutions(device, formats)
        
        # Get physical ID for de-duplication
        physical_id = self._get_physical_id(device)
        
        config = CameraConfig(
            device=device,
            format=selected_format,
            resolution=safe_resolutions[0] if safe_resolutions else "640x480",
            supported_formats=formats,
            safe_resolutions=safe_resolutions,
            driver_info=driver_info,
            physical_id=physical_id,
        )
        
        logger.info(
            "Probed %s (%s): format=%s, resolutions=%s, formats=%s",
            device, name, config.format, config.safe_resolutions, formats
        )
        
        return config
    
    def _get_device_info(self, device: str) -> tuple[str, str]:
        """
        Get device name and driver info from sysfs.
        
        Args:
            device: Device path
        
        Returns:
            Tuple of (name, driver_info)
        """
        name = "unknown"
        driver_info = "unknown"
        
        try:
            video_name = Path(f"/sys/class/video4linux/{Path(device).name}/name")
            if video_name.exists():
                name = video_name.read_text().strip()
            
            # Try to get driver info
            device_link = Path(f"/sys/class/video4linux/{Path(device).name}/device")
            if device_link.exists() and device_link.is_symlink():
                target = os.readlink(device_link)
                # Extract driver name from path
                driver_match = re.search(r'/drivers/([^/]+)', target)
                if driver_match:
                    driver_info = driver_match.group(1)
        except Exception as e:
            logger.debug("Could not get device info for %s: %s", device, e)
        
        return name, driver_info
    
    def _is_camera_device(self, name: str) -> bool:
        """
        Check if device is likely a camera (not a codec/decoder).
        
        Args:
            name: Device name
        
        Returns:
            True if likely a camera
        """
        name_lower = name.lower()
        non_camera_keywords = [
            "codec", "isp", "hevc", "h264", "h265", "encoder",
            "decoder", "component", "render", "display", "mipi"
        ]
        
        return not any(kw in name_lower for kw in non_camera_keywords)
    
    def _probe_formats(self, device: str) -> List[str]:
        """
        Probe supported formats using v4l2-ctl and ffmpeg.
        
        This is EPHEMERAL probing - never locks the device.
        
        Args:
            device: Device path
        
        Returns:
            List of supported format strings
        """
        formats = []
        
        # Try v4l2-ctl first (fast, doesn't lock)
        v4l2_formats = self._probe_v4l2ctl(device)
        formats.extend(v4l2_formats)
        
        # Try ffmpeg probe (also ephemeral with -list_formats)
        ffmpeg_formats = self._probe_ffmpeg(device)
        for fmt in ffmpeg_formats:
            if fmt not in formats:
                formats.append(fmt)
        
        return formats
    
    def _probe_v4l2ctl(self, device: str) -> List[str]:
        """
        Probe formats using v4l2-ctl --list-formats-ext.
        
        Args:
            device: Device path
        
        Returns:
            List of format strings
        """
        formats = []
        
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--device", device, "--list-formats-ext"],
                capture_output=True,
                text=True,
                timeout=self.PROBE_TIMEOUT,
            )
            
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    # Look for format codes like 'MJPG', 'YUYV', etc.
                    match = re.search(r"'([A-Z0-9]+)'", line)
                    if match:
                        fmt = match.group(1).lower()
                        if fmt in ["mjpg", "mjpeg"]:
                            fmt = "mjpeg"
                        elif fmt in ["yuyv", "yuyv422"]:
                            fmt = "yuyv422"
                        elif fmt == "h264":
                            fmt = "h264"
                        elif fmt == "nv12":
                            fmt = "nv12"
                        
                        if fmt in ["mjpeg", "yuyv422", "h264", "nv12"]:
                            if fmt not in formats:
                                formats.append(fmt)
        except FileNotFoundError:
            logger.debug("v4l2-ctl not available")
        except subprocess.TimeoutExpired:
            logger.debug("v4l2-ctl probe timed out for %s", device)
        except Exception as e:
            logger.debug("v4l2-ctl probe failed for %s: %s", device, e)
        
        return formats
    
    def _probe_ffmpeg(self, device: str) -> List[str]:
        """
        Probe formats using ffmpeg -list_formats all.
        
        Args:
            device: Device path
        
        Returns:
            List of format strings
        """
        formats = []
        
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-f", "v4l2",
                    "-list_formats", "all",
                    "-i", device,
                ],
                capture_output=True,
                text=True,
                timeout=self.PROBE_TIMEOUT,
            )
            
            # Parse format list from output
            output = result.stderr + result.stdout
            
            # Look for format codes in quotes
            matches = re.findall(r"'([A-Z0-9]+)'", output)
            for fmt in matches:
                fmt_lower = fmt.lower()
                if fmt_lower in ["mjpg", "mjpeg"]:
                    fmt_lower = "mjpeg"
                elif fmt_lower in ["yuyv", "yuyv422"]:
                    fmt_lower = "yuyv422"
                elif fmt_lower == "h264":
                    fmt_lower = "h264"
                elif fmt_lower == "nv12":
                    fmt_lower = "nv12"
                
                if fmt_lower in ["mjpeg", "yuyv422", "h264", "nv12"]:
                    if fmt_lower not in formats:
                        formats.append(fmt_lower)
        except FileNotFoundError:
            logger.debug("ffmpeg not available for format probing")
        except subprocess.TimeoutExpired:
            logger.debug("ffmpeg probe timed out for %s", device)
        except Exception as e:
            logger.debug("ffmpeg probe failed for %s: %s", device, e)
        
        return formats
    
    def _select_format(self, formats: List[str]) -> str:
        """
        Select best format from available formats.
        
        Args:
            formats: List of detected formats
        
        Returns:
            Selected format (or 'auto' if none detected)
        """
        fmt_lower = [f.lower() for f in formats]
        
        for priority_fmt in self.FORMAT_PRIORITY:
            if priority_fmt in fmt_lower:
                return priority_fmt
        
        # No supported format detected - will use auto-detection
        return "auto"
    
    def _determine_resolutions(self, device: str, formats: List[str]) -> List[str]:
        """
        Determine safe resolutions for device.
        
        Args:
            device: Device path
            formats: Detected formats
        
        Returns:
            List of safe resolutions
        """
        # Try to get resolutions from v4l2-ctl
        v4l2_resolutions = self._probe_v4l2ctl_resolutions(device)
        if v4l2_resolutions:
            return v4l2_resolutions
        
        # Use defaults based on camera type (will be set later)
        return ["640x480", "1280x720"]
    
    def _probe_v4l2ctl_resolutions(self, device: str) -> List[str]:
        """
        Probe resolutions using v4l2-ctl --list-formats-ext.
        
        Args:
            device: Device path
        
        Returns:
            List of resolution strings
        """
        resolutions = []
        
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--device", device, "--list-formats-ext"],
                capture_output=True,
                text=True,
                timeout=self.PROBE_TIMEOUT,
            )
            
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if "Size: Discrete" in line:
                        match = re.search(r'Size: Discrete (\d+x\d+)', line)
                        if match:
                            res = match.group(1)
                            if res not in resolutions:
                                resolutions.append(res)
        except Exception:
            pass
        
        return resolutions
    
    def _get_physical_id(self, device: str) -> str:
        """
        Get stable physical identifier for device.
        
        Args:
            device: Device path
        
        Returns:
            Physical ID string
        """
        # Prefer udev properties for stable identifiers
        try:
            props = self._get_udev_properties(device)
            if props:
                for key in ("ID_PATH", "ID_SERIAL_SHORT", "ID_SERIAL", "ID_USB_SERIAL", "ID_MODEL_ID"):
                    if key in props and props[key]:
                        return props[key]
        except Exception:
            pass

        try:
            video_dir = Path(f"/sys/class/video4linux/{Path(device).name}")
            device_link = video_dir / "device"

            if device_link.exists() and device_link.is_symlink():
                target = os.readlink(device_link)
                abs_target = (video_dir.parent / target).resolve()
                return str(abs_target)
        except Exception:
            pass

        return device

    def _get_udev_properties(self, device: str) -> Dict[str, str]:
        """
        Query udevadm for properties for a device.

        Returns a dict of KEY->VALUE. If udevadm is not present or fails,
        returns empty dict.
        """
        try:
            res = subprocess.run(
                ["udevadm", "info", "--query=property", "--name", device],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res.returncode != 0:
                return {}

            props: Dict[str, str] = {}
            for line in (res.stdout or "").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k.strip()] = v.strip()
            return props
        except FileNotFoundError:
            # udevadm not installed
            return {}
        except subprocess.TimeoutExpired:
            return {}
        except Exception:
            return {}
    
    def _classify_cameras(self, configs: List[CameraConfig]) -> None:
        """
        Classify cameras as internal or external.
        
        Args:
            configs: List of camera configs to classify
        """
        if not configs:
            return
        
        # First camera is internal, others are external
        for i, config in enumerate(configs):
            if i == 0:
                config.camera_type = "internal"
                # Use lower resolution for internal
                if config.safe_resolutions:
                    config.resolution = "640x480"
            else:
                config.camera_type = "external"
                # Use higher resolution for external
                if config.safe_resolutions:
                    config.resolution = config.safe_resolutions[0]
    
    def clear_cache(self) -> None:
        """Clear the configuration cache."""
        self._cache.clear()
        self._cache_time.clear()
        logger.info("Camera config cache cleared")

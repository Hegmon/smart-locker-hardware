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
        List available USB V4L2 video devices with robust enumeration.

        Strategy:
        1. Enumerate /dev/v4l/by-id/* symlinks (preferred - stable USB IDs)
        2. Filter to USB devices only
        3. Resolve symlinks to /dev/videoX
        4. Filter out metadata nodes (index1, index3, etc.)
        5. Fallback to /dev/video* scanning if by-id fails
        6. Validate USB parent for fallback devices

        Returns:
            Sorted list of validated USB video device paths
        """
        devices: List[str] = []

        # Method 1: Use stable by-id symlinks (preferred for USB cameras)
        try:
            by_id_dir = Path("/dev/v4l/by-id")
            if by_id_dir.exists():
                logger.debug("Enumerating devices from /dev/v4l/by-id")
                for entry in sorted(by_id_dir.iterdir()):
                    try:
                        if not entry.is_symlink():
                            continue

                        # Check if this looks like a USB device symlink
                        name = entry.name
                        if not (name.startswith("usb-") and "video-index" in name):
                            continue

                        # Resolve symlink
                        resolved = entry.resolve()
                        device_path = str(resolved)

                        # Validate it's a video device
                        if not device_path.startswith("/dev/video"):
                            continue

                        # Filter out metadata nodes (index1, index3, etc.)
                        if self._is_metadata_node(device_path):
                            logger.debug("Skipping metadata node: %s", device_path)
                            continue

                        # Quick USB validation
                        if self._is_usb_device(device_path):
                            if device_path not in devices:
                                devices.append(device_path)
                                logger.debug("Added USB device from by-id: %s -> %s", entry.name, device_path)
                        else:
                            logger.debug("Skipping non-USB device from by-id: %s", device_path)

                    except Exception as e:
                        logger.debug("Error processing by-id entry %s: %s", entry.name, e)
                        continue
        except Exception as e:
            logger.debug("Failed to enumerate /dev/v4l/by-id: %s", e)

        # Method 2: Fallback to by-path symlinks
        if not devices:
            try:
                by_path_dir = Path("/dev/v4l/by-path")
                if by_path_dir.exists():
                    logger.debug("Falling back to /dev/v4l/by-path")
                    for entry in sorted(by_path_dir.iterdir()):
                        try:
                            if not entry.is_symlink():
                                continue

                            name = entry.name
                            if not ("usb-" in name and "video-index" in name):
                                continue

                            resolved = entry.resolve()
                            device_path = str(resolved)

                            if not device_path.startswith("/dev/video"):
                                continue

                            if self._is_metadata_node(device_path):
                                continue

                            if self._is_usb_device(device_path):
                                if device_path not in devices:
                                    devices.append(device_path)
                                    logger.debug("Added USB device from by-path: %s -> %s", name, device_path)

                        except Exception as e:
                            logger.debug("Error processing by-path entry %s: %s", entry.name, e)
                            continue
            except Exception as e:
                logger.debug("Failed to enumerate /dev/v4l/by-path: %s", e)

        # Method 3: Final fallback to direct /dev/video* enumeration
        if not devices:
            logger.debug("Falling back to direct /dev/video* enumeration")
            try:
                for device_path in glob("/dev/video*"):
                    try:
                        # Skip if already found via by-id/by-path
                        if device_path in devices:
                            continue

                        # Filter out metadata nodes
                        if self._is_metadata_node(device_path):
                            continue

                        # Validate USB connection
                        if self._is_usb_device(device_path):
                            devices.append(device_path)
                            logger.debug("Added USB device from direct scan: %s", device_path)
                        else:
                            logger.debug("Skipping non-USB device: %s", device_path)

                    except Exception as e:
                        logger.debug("Error checking device %s: %s", device_path, e)
                        continue
            except Exception as e:
                logger.debug("Failed direct video device enumeration: %s", e)

        # Sort by device number (video0, video1, ...)
        def _num_key(dev: str) -> int:
            m = re.search(r"(\d+)$", dev)
            try:
                return int(m.group(1)) if m else 999
            except Exception:
                return 999

        devices.sort(key=_num_key)
        logger.debug("Found %d USB video devices: %s", len(devices), devices)
        return devices

    def _is_metadata_node(self, device_path: str) -> bool:
        """
        Check if device is a metadata-only node (not a capture node).

        USB cameras often expose multiple /dev/videoX nodes:
        - video-index0: Primary capture node
        - video-index1: Metadata/control node (UVC extension units)
        - video-index2+: Additional capture nodes (rare)

        Args:
            device_path: Device path

        Returns:
            True if this is a metadata node that should be skipped
        """
        try:
            # Check device name pattern
            device_name = Path(device_path).name

            # If device path contains metadata indicators
            if "metadata" in device_name.lower():
                return True

            # Check udev properties for metadata indicators
            props = self._get_udev_properties(device_path)
            if props:
                capabilities = props.get("ID_V4L_CAPABILITIES", "")
                if ":metadata:" in capabilities and ":capture:" not in capabilities:
                    return True

            # Heuristic: for USB cameras, odd-numbered nodes are often metadata
            # video0 = capture, video1 = metadata, video2 = capture, video3 = metadata, etc.
            match = re.search(r"video(\d+)$", device_name)
            if match:
                index = int(match.group(1))
                # Check if this is an odd index (1, 3, 5, ...)
                if index > 0 and index % 2 == 1:
                    # Additional validation: check if paired even node exists
                    even_device = f"/dev/video{index - 1}"
                    if os.path.exists(even_device):
                        logger.debug("Likely metadata node: %s (paired with %s)", device_path, even_device)
                        return True

        except Exception as e:
            logger.debug("Error checking metadata status for %s: %s", device_path, e)

        return False
    
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
        is_usb_device = self._is_usb_device(device, udev)
        if not is_usb_device:
            logger.info("Skipping non-USB device: %s (%s)", device, name)
            return None

        # Skip non-camera devices
        if not self._is_camera_device(name):
            logger.info("Skipping non-camera device: %s (%s)", device, name)
            return None
        
        # Probe formats (ephemeral - never locks device)
        formats = self._probe_formats(device)

        # Validate device with frame capture test
        is_valid, validation_reason = self._validate_device_capture(device)
        if not is_valid:
            logger.warning("Device validation failed for %s (%s): %s", device, name, validation_reason)
            return None

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
    
    def _is_usb_device(self, device: str, udev_props: Optional[Dict[str, str]] = None) -> bool:
        """
        Check if a device is connected via USB bus.

        Uses multiple detection methods:
        1. udev properties (ID_BUS=usb)
        2. sysfs USB parent traversal
        3. V4L2 device capabilities

        Args:
            device: Device path (e.g., /dev/video0)
            udev_props: Pre-fetched udev properties (optional)

        Returns:
            True if device is USB-connected
        """
        logger.debug("Checking USB status for device: %s", device)

        # Method 1: Check udev properties
        if udev_props:
            id_bus = udev_props.get("ID_BUS", "").lower()
            if id_bus == "usb":
                logger.debug("USB device confirmed via udev ID_BUS: %s", device)
                return True
            elif id_bus and id_bus != "usb":
                logger.debug("Non-USB device via udev ID_BUS=%s: %s", id_bus, device)
                return False

        # Method 2: Get udev properties if not provided
        if not udev_props:
            try:
                udev_props = self._get_udev_properties(device)
                if udev_props:
                    id_bus = udev_props.get("ID_BUS", "").lower()
                    if id_bus == "usb":
                        logger.debug("USB device confirmed via udev ID_BUS: %s", device)
                        return True
                    elif id_bus and id_bus != "usb":
                        logger.debug("Non-USB device via udev ID_BUS=%s: %s", id_bus, device)
                        return False
            except Exception as e:
                logger.debug("Failed to get udev properties for %s: %s", device, e)

        # Method 3: sysfs USB parent traversal
        try:
            usb_parent = self._find_usb_parent(device)
            if usb_parent:
                logger.debug("USB device confirmed via sysfs parent: %s -> %s", device, usb_parent)
                return True
        except Exception as e:
            logger.debug("Failed sysfs USB check for %s: %s", device, e)

        # Method 4: Check V4L2 capabilities for USB indicators
        try:
            caps = self._get_v4l2_capabilities(device)
            if ":capture:" in caps and self._has_usb_capabilities(device):
                logger.debug("USB device confirmed via V4L2 capabilities: %s", device)
                return True
        except Exception as e:
            logger.debug("Failed V4L2 capability check for %s: %s", device, e)

        logger.debug("Unable to confirm USB status for device: %s", device)
        return False

    def _find_usb_parent(self, device: str) -> Optional[str]:
        """
        Traverse sysfs to find USB parent device.

        Args:
            device: Device path

        Returns:
            USB parent path if found, None otherwise
        """
        try:
            video_dir = Path(f"/sys/class/video4linux/{Path(device).name}")
            device_link = video_dir / "device"

            if not device_link.exists() or not device_link.is_symlink():
                return None

            # Resolve the device symlink
            target = device_link.resolve()

            # Walk up the device tree looking for USB parent
            current = target
            for _ in range(15):  # Max depth to prevent infinite loops
                if str(current).startswith("/sys/devices/") and "/usb" in str(current):
                    return str(current)

                # Check if current directory has usb in name
                if "usb" in current.name.lower():
                    return str(current)

                # Move up one level
                parent = current.parent
                if parent == current:  # Reached root
                    break
                current = parent

        except Exception:
            pass

        return None

    def _get_v4l2_capabilities(self, device: str) -> str:
        """
        Get V4L2 device capabilities.

        Args:
            device: Device path

        Returns:
            Capabilities string
        """
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--device", device, "--info"],
                capture_output=True,
                text=True,
                timeout=2,
            )

            if result.returncode == 0:
                return result.stdout.lower()
        except Exception:
            pass

        return ""

    def _has_usb_capabilities(self, device: str) -> bool:
        """
        Check if device has USB-specific capabilities.

        Args:
            device: Device path

        Returns:
            True if USB capabilities detected
        """
        try:
            # Check if device name suggests USB (from by-id/by-path)
            device_path = Path(device)
            if "by-id" in str(device_path) or "by-path" in str(device_path):
                return True

            # Check udev properties for USB indicators
            props = self._get_udev_properties(device)
            if props:
                usb_indicators = ["ID_USB", "ID_SERIAL", "ID_VENDOR_ID", "ID_MODEL_ID"]
                if any(key in props for key in usb_indicators):
                    return True

        except Exception:
            pass

        return False

    def _validate_device_capture(self, device: str) -> tuple[bool, str]:
        """
        Validate device can capture frames using a short test capture.

        Args:
            device: Device path

        Returns:
            Tuple of (is_valid, reason)
        """
        try:
            # Use ffmpeg to attempt a very short capture test
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-f", "v4l2",
                    "-framerate", "5",
                    "-video_size", "320x240",  # Small size for quick test
                    "-i", device,
                    "-frames:v", "1",  # Just one frame
                    "-f", "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=10,  # Short timeout
            )

            if result.returncode == 0:
                return True, "Frame capture successful"

            # Analyze error output
            error_output = (result.stderr + result.stdout).lower()

            # Common failure reasons
            if "device or resource busy" in error_output:
                return False, "Device busy"
            elif "no such device" in error_output or "no such file" in error_output:
                return False, "Device not found"
            elif "permission denied" in error_output:
                return False, "Permission denied"
            elif "inappropriate ioctl" in error_output:
                return False, "Not a capture device"
            elif "timeout" in error_output:
                return False, "Device timeout"
            else:
                return False, f"Capture failed: {error_output[:100]}"

        except subprocess.TimeoutExpired:
            return False, "Capture timeout"
        except FileNotFoundError:
            return False, "ffmpeg not available"
        except Exception as e:
            return False, f"Validation error: {str(e)}"

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

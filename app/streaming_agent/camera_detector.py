"""
Camera Detection and Classification with Format Probing
Auto-detects valid camera devices, filters out codec/media nodes,
and selects optimal cameras based on format support (MJPEG > YUYV > H264).

Strategy:
- Use FFmpeg-based format probing (not just V4L2 enumeration)
- Filter out codec/ISP/HEVC devices by name heuristics
- Prefer MJPEG-capable devices for internal/external roles
- Fallback to YUYV if MJPEG unavailable
- Support explicit overrides via INTERNAL_CAMERA_DEVICE / EXTERNAL_CAMERA_DEVICE
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from glob import glob
from pathlib import Path
from typing import NamedTuple, Optional, List

from .device_config import get_optional_config

logger = logging.getLogger(__name__)


class CameraInfo(NamedTuple):
    """Information about a detected camera with format support"""
    device_path: str
    camera_type: str  # "internal" or "external"
    index: int
    formats: List[str]  # supported pixel formats
    name: str  # friendly name from sysfs
    resolutions: List[str] = []
    reason: str = ""
    backend: str = "v4l2"  # "libcamera" or "v4l2"


class CameraDetector:
    """Detects, classifies, and selects optimal cameras for streaming"""

    # Keywords that indicate a non-camera device
    NON_CAMERA_KEYWORDS = [
        "codec", "isp", "hevc", "h264", "h265", "encoder",
        "decoder", "component", "render", "display"
    ]

    # Name keywords that suggest integrated/built-in camera
    INTERNAL_NAME_KEYWORDS = [
        "integrated", "built-in", "internal", "onboard", "imx"
    ]

    # Preferred formats (ordered by preference)
    PREFERRED_FORMATS = ["mjpeg", "mjpg", "yuyv", "yuyv422", "yuv422", "h264"]

    FORMAT_PROBE_TIMEOUT = 3  # seconds

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

    def _list_v4l2_devices(self) -> list[str]:
        """Enumerate /dev/video nodes from v4l2-ctl, falling back to glob.
        
        Returns sorted list of unique /dev/videoX paths.
        """
        devices: list[str] = []
        
        # Try v4l2-ctl first
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--list-devices"],
                capture_output=True,
                text=True,
                timeout=self.FORMAT_PROBE_TIMEOUT,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    candidate = line.strip()
                    if candidate.startswith("/dev/video"):
                        devices.append(candidate)
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            pass

        # Fallback to glob
        if not devices:
            devices = glob("/dev/video*")

        # Remove duplicates and sort
        devices = sorted(set(devices), key=self._device_sort_key)
        
        return devices

    @staticmethod
    def _device_sort_key(device_path: str) -> tuple[int, str]:
        try:
            return (int(device_path.replace("/dev/video", "")), device_path)
        except ValueError:
            return (999, device_path)

    def _get_device_name(self, device_path: str) -> str:
        """Read the video device's friendly name from sysfs."""
        try:
            name_file = Path(f"/sys/class/video4linux/{Path(device_path).name}/name")
            if name_file.exists():
                return name_file.read_text().strip()
        except Exception:
            pass
        return ""

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

    def _get_device_bus(self, device_path: str) -> Optional[str]:
        """
        Determine the bus type of a video device using multiple methods.

        Returns 'usb', 'platform', 'pci', or None if cannot determine.
        """
        try:
            # Method 1: udev properties (most reliable)
            props = self._get_udev_properties(device_path)
            if props:
                bus = props.get('ID_BUS', '').lower()
                if bus in ('usb', 'platform', 'pci'):
                    return bus

            # Method 2: sysfs traversal from device link
            video_dir = Path(f"/sys/class/video4linux/{Path(device_path).name}")
            if video_dir.exists():
                device_link = video_dir / "device"
                if device_link.exists() and device_link.is_symlink():
                    target = device_link.resolve()

                    # Walk up the device tree
                    current = target
                    for _ in range(15):  # Max depth
                        path_str = str(current)

                        # Check for bus indicators in path
                        if "/usb" in path_str or current.name.startswith("usb"):
                            return "usb"
                        elif "/platform" in path_str or current.name.startswith("platform"):
                            return "platform"
                        elif "/pci" in path_str or current.name.startswith("pci"):
                            return "pci"

                        # Move up
                        parent = current.parent
                        if parent == current:
                            break
                        current = parent

            # Method 3: Fallback sysfs tree walk
            return self._detect_bus_from_sysfs_tree(device_path)

        except Exception as e:
            logger.debug("Error determining bus for %s: %s", device_path, e)
            return None

    def _detect_bus_from_sysfs_tree(self, device_path: str) -> Optional[str]:
        """
        Alternative bus detection by walking sysfs tree from multiple entry points.
        """
        try:
            dev_name = Path(device_path).name

            # Try multiple sysfs entry points
            possible_paths = [
                f"/sys/class/video4linux/{dev_name}",
                f"/sys/devices/virtual/video4linux/{dev_name}",
            ]

            for sysfs_path in possible_paths:
                path_obj = Path(sysfs_path)
                if not path_obj.exists():
                    continue

                # Walk up from this entry point
                current = path_obj
                for _ in range(20):  # Allow deeper traversal
                    path_str = str(current)

                    # Check for bus indicators
                    if "/usb" in path_str or "usb" in current.name.lower():
                        return "usb"
                    elif "/platform" in path_str or "platform" in current.name.lower():
                        return "platform"
                    elif "/pci" in path_str or "pci" in current.name.lower():
                        return "pci"

                    # Also check for specific bus directory names
                    if current.name in ("usb1", "usb2", "usb3", "usb4"):
                        return "usb"

                    # Move up
                    parent = current.parent
                    if parent == current or str(parent) == "/":
                        break
                    current = parent

            return None
        except Exception as e:
            logger.debug("Sysfs tree walk failed for %s: %s", device_path, e)
            return None

    def _get_physical_device_id(self, device_path: str) -> Optional[str]:
        """
        Get a stable identifier for the physical device (e.g., USB bus address).
        Returns None if unavailable.
        """
        try:
            video_dir = Path(f"/sys/class/video4linux/{Path(device_path).name}")
            if not video_dir.exists():
                return None
            device_link = video_dir / "device"
            if not device_link.exists() or not device_link.is_symlink():
                return None
            # Resolve to physical device path, strip /sys/fs prefix
            target = os.readlink(device_link)
            # Convert to absolute path under /sys
            abs_target = (video_dir.parent / target).resolve()
            # The physical device is usually a few levels up from videoX
            # e.g., .../devices/pci0000:00/.../3-6:1.0 -> use 3-6:1.0
            # We'll use the full resolved path as a unique ID
            return str(abs_target)
        except Exception:
            return None

    def _classify_camera_backend(self, device_path: str, name: str) -> str:
        """
        Classify camera backend type based on device name and sysfs info.

        Returns:
            "libcamera" for CSI/unicam cameras
            "v4l2" for USB and standard V4L2 cameras
        """
        name_lower = name.lower()

        # Check for libcamera/CSI indicators
        libcamera_indicators = [
            "unicam", "csi", "imx", "ov", "raspberry pi camera",
            "libcamera", "vc4", "bcm2835"
        ]

        if any(indicator in name_lower for indicator in libcamera_indicators):
            logger.info("Detected libcamera backend for %s (%s)", device_path, name)
            return "libcamera"

        # Check sysfs for platform devices (typically CSI)
        try:
            video_dir = Path(f"/sys/class/video4linux/{Path(device_path).name}")
            if video_dir.exists():
                device_link = video_dir / "device"
                if device_link.exists() and device_link.is_symlink():
                    target = os.readlink(device_link)
                    if "/platform" in target:
                        logger.info("Detected libcamera backend (platform device) for %s", device_path)
                        return "libcamera"
        except Exception:
            pass

        # Default to V4L2 for USB and other devices
        logger.info("Detected v4l2 backend for %s (%s)", device_path, name)
        return "v4l2"

    def _probe_formats(self, device_path: str) -> List[str]:
        """Probe supported formats using v4l2-ctl and ffmpeg."""
        from .ffmpeg_manager import FormatScanner

        # Use FormatScanner for format detection
        profile = FormatScanner.probe_device(device_path)
        formats = profile.supported_formats

        # If no formats detected but device is valid, provide USB camera defaults
        if not formats:
            # Check if this is a USB camera by device path
            if 'usb-' in device_path or self._confirm_usb_device(device_path):
                logger.info("No formats detected for USB camera %s, using defaults", device_path)
                formats = ["mjpeg", "yuyv422"]  # Common USB camera formats

        return formats
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "v4l2", "-list_formats", "all",
                    "-i", device_path
                ],
                capture_output=True, text=True, timeout=self.FORMAT_PROBE_TIMEOUT
            )
            # Parse format list from stderr
            output = result.stderr + result.stdout
            for line in output.splitlines():
                line = line.strip().lower()
                # Look for format codes like: 'mjpg' / 'mjpeg' / 'yuyv422' etc.
                if "'" in line:
                    parts = line.split("'")
                    if len(parts) >= 2:
                        fmt = parts[1]
                        # Only include known raw/compressed formats
                        if fmt not in formats:
                            formats.append(fmt)
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            pass

        return formats

    def _probe_resolutions(self, device_path: str) -> List[str]:
        """Return known frame sizes from v4l2-ctl."""
        resolutions: list[str] = []
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--device", device_path, "--list-formats-ext"],
                capture_output=True,
                text=True,
                timeout=self.FORMAT_PROBE_TIMEOUT,
            )
            if result.returncode != 0:
                return resolutions

            for line in result.stdout.splitlines():
                line = line.strip()
                marker = "Size: Discrete "
                if marker in line:
                    size = line.split(marker, 1)[1].strip()
                    if size and size not in resolutions:
                        resolutions.append(size)
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            pass
        return resolutions

    def _preferred_capture_format(self, formats: List[str]) -> Optional[str]:
        """
        Select preferred capture format from supported formats.
        Returns None if no known format is found - FFmpeg will use auto-detection.
        """
        if not formats:
            # Empty format list is OK - FFmpeg can auto-detect
            return None
        
        fmt_lower = [f.lower() for f in formats]
        if "mjpeg" in fmt_lower or "mjpg" in fmt_lower:
            return "mjpeg"
        if "yuyv" in fmt_lower or "yuyv422" in fmt_lower or "yuv422" in fmt_lower:
            return "yuyv422"
        if "h264" in fmt_lower:
            return "h264"
        return None

    def _ffmpeg_can_open(self, device_path: str, formats: List[str]) -> tuple[bool, str, int]:
        """
        Probe whether FFmpeg can open and read frames from the device.
        
        A device is VALID if:
        - /dev/videoX exists
        - supports VIDEO_CAPTURE capability  
        - FFmpeg can start successfully
        - produces at least one frame
        
        Returns (is_valid, reason)
        """
        # Check device exists
        if not os.path.exists(device_path):
            return False, "Device not found"
        
        input_format = self._preferred_capture_format(formats)
        
        # Build probe command: capture 1 frame, fail fast on errors
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "v4l2",
            "-framerate", "5",  # Slow, safe framerate for probe
            "-video_size", "640x480",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-max_delay", "5",
        ]
        
        if input_format:
            cmd.extend(["-input_format", input_format])
        
        cmd.extend([
            "-i", device_path,
            "-frames:v", "1",  # Just one frame
            "-f", "null",
            "-",
        ])
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            output = (result.stderr or result.stdout or "").strip()

            # Success: captured one frame
            if result.returncode == 0:
                return True, f"OK: captured frame (format: {input_format or 'auto'})", 0

            output_lower = output.lower()

            # Device busy - transient error, device itself is valid but lower priority
            if "device or resource busy" in output_lower or "resource busy" in output_lower:
                return True, "Device busy (valid but in use)", 1

            # MJPEG warnings are non-fatal - camera is valid but lower priority
            mjpeg_warnings = ["unable to decode", "deprecated pixel format", "first field"]
            if any(w in output_lower for w in mjpeg_warnings):
                return True, f"MJPEG warning (valid): {output[:100]}", 1

            # Fatal errors - device cannot be used as capture source
            fatal_errors = [
                "inappropriate ioctl for device",
                "not a video capture device",
                "no such device",
                "permission denied",
                "invalid argument",
                "no medium found",
                "function not implemented",
                "ioctl(vidioc_g_input)",  # Metadata/control nodes (lowercased)
                "ioctl(vidioc_querycap)",  # Device doesn't support capture
            ]

            for err in fatal_errors:
                if err in output_lower:
                    return False, f"Invalid device: {err}", 2

            # Unknown error - be conservative, reject
            return False, f"Probe failed: {output[:200] if output else 'no output'}", 2

        except FileNotFoundError:
            # ffmpeg not installed: cannot probe — treat as medium confidence valid
            return True, "ffmpeg not installed, skipping probe", 1
        except subprocess.TimeoutExpired:
            return False, "Probe timeout - device unresponsive", 2
        except Exception as exc:
            return False, str(exc)[:100], 2

    def _is_camera_device(self, device_path: str, name: str) -> bool:
        """Check if device is likely a camera (not codec/ISP/misc).
        
        Filters out:
        - Codec/ISP/encoder/decoder devices
        - Metadata/control nodes (secondary video nodes)
        - Known non-capture devices
        
        Strategy:
        1. Check name for known non-camera keywords
        2. Check device path for known non-camera subsystems  
        3. Prefer primary capture nodes (even indices for multi-node cameras)
        """
        name_lower = name.lower()
        
        # Known non-camera device keywords from Raspberry Pi
        non_camera_keywords = [
            "codec",
            "isp",
            "hevc",
            "h264",
            "h265",
            "encoder",
            "decoder",
            "component",
            "render",
            "display",
            "metadata", 
            "control",
            "streamer",
            "raw",
            "stat",
        ]
        
        if any(kw in name_lower for kw in non_camera_keywords):
            return False
            
        # Check device path for known non-camera subsystems
        known_bad_paths = [
            "media",
            "bcm2835-codec",
            "bcm2835-isp",
            "rpi-hevc-dec",
        ]
        
        for bad in known_bad_paths:
            if bad in device_path.lower():
                return False
        
        # USB cameras come in pairs: /dev/videoX (capture) and /dev/videoX+1 (metadata)
        # Heuristic: Filter out odd-numbered nodes from USB cameras since they're metadata
        try:
            dev_num = int(device_path.replace("/dev/video", ""))
            # Check if this is likely a metadata node (odd number after first capture node)
            # For most USB cameras: /dev/video0 is capture, /dev/video1 is metadata
            #                       /dev/video2 is capture, /dev/video3 is metadata, etc.
            if dev_num > 0 and dev_num % 2 == 1:
                # This is an odd-numbered node, likely metadata for the previous device
                # But only filter if it's for a USB camera (check bus type)
                bus = self._get_device_bus(device_path)
                if bus == "usb":
                    logger.debug("Filtering out likely metadata node: %s (%s)", device_path, name)
                    return False
        except (ValueError, AttributeError):
            pass
                
        return True

    def _find_best_device_for_role(self, candidates: List[str],
                                    role: str,
                                    override: Optional[str]) -> Optional[str]:
        """
        Select the best device for internal/external role.
        
        Scoring prioritizes:
        1. Explicit override
        2. Device that is /dev/video0 (typically internal)
        3. Bus type (platform > usb)
        4. Device index (lower is better)
        
        Format enumeration is NOT used for selection since it can be incomplete.
        Instead, we rely on the ffmpeg_can_open probe which tests actual streaming.
        """
        if override and override in candidates:
            return override

        # Score devices based on bus type and device index
        # Lower score = better
        scored = []
        for dev in candidates:
            name = self._get_device_name(dev)
            bus = self._get_device_bus(dev)
            
            # Score: lower is better
            score = 0
            
            # Prefer platform (built-in) over USB
            if bus == "platform":
                score += 0
            elif bus == "usb":
                score += 100
            else:
                score += 200
            
            # Prefer lower device number (e.g., /dev/video0)
            try:
                dev_num = int(dev.replace("/dev/video", ""))
                score += dev_num * 10
            except ValueError:
                score += 999
            
            # Bonus for internal name keywords
            name_lower = name.lower()
            if any(kw in name_lower for kw in self.INTERNAL_NAME_KEYWORDS):
                score -= 50
            
            scored.append((score, dev, name))

        # Sort by score ascending, then by device index
        scored.sort(key=lambda x: (x[0], self._device_sort_key(x[1])))

        if scored:
            best = scored[0]
            logger.info(
                "Selected %s camera: %s (%s), score=%s",
                role,
                best[1],
                best[2],
                best[0],
            )
            return best[1]
        return None

    def _resolve_roles(self, candidates: dict[str, str],
                       candidate_names: dict[str, str],
                       candidate_formats: dict[str, List[str]]) -> dict[str, str]:
        """
        Determine which physical camera is internal vs external.
        
        Strategy:
        1. Handle explicit overrides
        2. Classify remaining by bus type (platform = internal, usb = external)
        3. Use name heuristics for USB cameras
        4. Pick best device based on bus and device index
        
        Format enumeration is NOT used for role resolution.
        """
        internal_phys = None
        external_phys = None

        # Handle explicit overrides - map device path to phys_id
        if self.override_internal:
            for phys_id, dev in candidates.items():
                if dev == self.override_internal:
                    internal_phys = phys_id
                    break
        if self.override_external:
            for phys_id, dev in candidates.items():
                if dev == self.override_external:
                    external_phys = phys_id
                    break

        # Pool of remaining physical cameras
        remaining_phys = [p for p in candidates.keys()
                         if p not in {internal_phys, external_phys}]

        # Classify remaining by bus
        platform_phys = []
        usb_phys = []
        for phys_id in remaining_phys:
            dev_node = candidates[phys_id]
            bus = self._get_device_bus(dev_node)
            if bus == "platform":
                platform_phys.append(phys_id)
            elif bus == "usb":
                usb_phys.append(phys_id)

        # Assign internal
        if not internal_phys:
            if platform_phys:
                internal_phys = self._pick_best_camera(platform_phys, candidates, candidate_formats)
            elif usb_phys:
                # Name heuristic among USB
                for phys_id in usb_phys:
                    name = candidate_names[phys_id].lower()
                    if any(kw in name for kw in self.INTERNAL_NAME_KEYWORDS):
                        internal_phys = phys_id
                        break
                if not internal_phys:
                    internal_phys = self._pick_best_camera(usb_phys, candidates, candidate_formats)

        # Assign external from remaining
        if not external_phys:
            pool = [p for p in remaining_phys if p != internal_phys]
            if pool:
                external_phys = self._pick_best_camera(pool, candidates, candidate_formats)

        result = {}
        if internal_phys:
            result["internal"] = internal_phys
        if external_phys:
            result["external"] = external_phys
        return result

    def _pick_best_camera(self, phys_ids: List[str],
                          candidates: dict[str, str],
                          candidate_formats: dict[str, List[str]]) -> Optional[str]:
        """
        Select best camera from list.
        
        Prioritizes:
        1. /dev/video0 (typically internal/built-in)
        2. Lower device index
        
        Format enumeration is not used for selection since it can be incomplete
        and the ffmpeg probe already validated the device.
        """
        best_phys = None
        best_score = 999

        for phys_id in phys_ids:
            dev_path = candidates[phys_id]
            
            # Score: lower is better
            score = 0
            
            # Strong preference for /dev/video0
            if dev_path == "/dev/video0":
                score -= 1000
            
            # Prefer lower device number
            try:
                dev_num = int(dev_path.replace("/dev/video", ""))
                score += dev_num
            except ValueError:
                score += 999
            
            if score < best_score:
                best_score = score
                best_phys = phys_id

        return best_phys if best_phys else (phys_ids[0] if phys_ids else None)

    def _resolve_internal_external(self, valid_cameras: List[str]) -> dict:
        """
        Determine which camera is internal vs external.
        
        Strategy:
        1. Handle explicit overrides
        2. Classify remaining by bus type (platform = internal, usb = external)
        3. Use name heuristics for USB cameras
        4. Pick best device based on bus and device index
        
        Format enumeration is NOT used for role resolution.
        """
        internal = None
        external = None

        # Handle explicit overrides first
        if self.override_internal and self.override_internal in valid_cameras:
            internal = self.override_internal
        if self.override_external and self.override_external in valid_cameras:
            external = self.override_external

        # Remove overrides from pool
        remaining = [dev for dev in valid_cameras
                     if dev not in {internal, external}]

        # Classify remaining by bus type
        platform_cams = []
        usb_cams = []
        for dev in remaining:
            bus = self._get_device_bus(dev)
            if bus == "platform":
                platform_cams.append(dev)
            elif bus == "usb":
                usb_cams.append(dev)

        # Strategy:
        # - If we already have internal from override: fine
        # - Else: pick platform cameras first; if none, use name heuristic on USB
        if not internal:
            if platform_cams:
                internal = self._find_best_device_for_role(
                    platform_cams, "internal", None)
            elif usb_cams:
                # Try to identify built-in USB camera by name
                built_in = None
                for dev in usb_cams:
                    name = self._get_device_name(dev).lower()
                    if any(kw in name for kw in self.INTERNAL_NAME_KEYWORDS):
                        built_in = dev
                        break
                internal = built_in or usb_cams[0]

        # Assign external from remaining pool
        if not external:
            pool = [d for d in remaining if d != internal]
            if pool:
                external = self._find_best_device_for_role(pool, "external", None)

        result = {}
        if internal:
            result["internal"] = internal
        if external:
            result["external"] = external
        return result

    def detect_cameras(self) -> List[CameraInfo]:
        """
        Detect all valid camera devices, de-duplicate physical devices,
        classify as internal/external, and select best node per camera.
        """
        # Legacy behavior kept for role-aware detection, but we now prefer
        # enumerating stable by-id links for multi-camera usage.
        all_devices = self._list_v4l2_devices()
        
        # Ensure /dev/video0 is tested first if present
        all_devices.sort(key=self._device_sort_key)
        
        logger.info("=== Camera Detection: Probing %d device(s) ===", len(all_devices))

        # Step 1: Gather candidate nodes and compute physical device ID
        candidates: dict[str, str] = {}   # phys_id -> best node path
        candidate_names: dict[str, str] = {}   # phys_id -> friendly name
        candidate_formats: dict[str, List[str]] = {}   # phys_id -> formats
        candidate_resolutions: dict[str, List[str]] = {}   # phys_id -> resolutions
        candidate_reasons: dict[str, str] = {}   # phys_id -> validation reason
        candidate_ffmpeg_results: dict[str, str] = {}   # phys_id -> ffmpeg probe result
        candidate_backends: dict[str, str] = {}   # phys_id -> backend type

        for dev in all_devices:
            name = self._get_device_name(dev)

            # Filter out non-camera devices early
            if not self._is_camera_device(dev, name):
                logger.info("Skipping non-camera device: %s (%s)", dev, name)
                continue

            # Classify backend type BEFORE probing
            backend = self._classify_camera_backend(dev, name)

            # Handle backend-specific probing
            if backend == "libcamera":
                # For libcamera devices, skip V4L2 probing entirely
                # These devices don't respond to V4L2 ioctl calls
                formats = []
                can_open = True
                ffmpeg_reason = f"libcamera backend detected, skipping V4L2 probe"
                logger.info("Device %s (%s) backend=libcamera, skipping V4L2 probing", dev, name)
            else:
                # For V4L2 devices, use existing probing logic
                formats = self._probe_formats(dev)
                logger.info("Device %s (%s) formats from enumeration: %s", dev, name, formats)

                # CRITICAL FIX: Do NOT reject cameras based solely on format enumeration
                # Always attempt ffmpeg probe regardless of format list results
                # This handles cases where v4l2-ctl enumeration is incomplete but ffmpeg works

                can_open, ffmpeg_reason, probe_rank = self._ffmpeg_can_open(dev, formats)

                # Log ffmpeg test result for debugging
                logger.info("Device %s (%s) ffmpeg probe: can_open=%s, reason=%s, rank=%s",
                            dev, name, can_open, ffmpeg_reason, probe_rank)

            if not can_open:
                logger.info("Skipping invalid camera node: %s (%s), %s", dev, name, ffmpeg_reason)
                continue

            resolutions = self._probe_resolutions(dev) if backend == "v4l2" else []

            # Compute physical device ID (same for all /dev/videoX nodes of same physical device)
            phys_id = self._get_physical_device_id(dev) or dev  # fallback to dev path

            # Score this node based on device index and bus type
            # NOT based on format enumeration (which can be incomplete)
            # Lower score = better
            def node_score(dev_path: str) -> int:
                score = 0
                # Strong preference for /dev/video0 (typically internal)
                if dev_path == "/dev/video0":
                    score -= 1000
                # Prefer lower device number
                try:
                    dev_num = int(dev_path.replace("/dev/video", ""))
                    score += dev_num * 10
                except ValueError:
                    score += 999
                return score

            current_score = node_score(dev)
            existing_score = node_score(candidates.get(phys_id, ""))

            # Prefer nodes with better probe rank (lower is better), then by node score
            existing_rank = candidate_ffmpeg_results.get(phys_id + "::rank")
            if existing_rank is None:
                existing_rank = 9

            should_replace = False
            # If we have a probe rank, prefer lower (better) rank
            if phys_id not in candidates:
                should_replace = True
            elif probe_rank is not None and probe_rank < existing_rank:
                should_replace = True
            elif probe_rank == existing_rank and current_score < existing_score:
                should_replace = True

            if should_replace:
                candidates[phys_id] = dev
                candidate_names[phys_id] = name
                candidate_formats[phys_id] = formats
                candidate_resolutions[phys_id] = resolutions
                candidate_reasons[phys_id] = ffmpeg_reason
                candidate_ffmpeg_results[phys_id] = ffmpeg_reason
                # store rank alongside results for comparison
                candidate_ffmpeg_results[phys_id + "::rank"] = probe_rank
                candidate_backends[phys_id] = backend
                candidate_backends[phys_id] = backend

        if not candidates:
            logger.warning("No valid camera devices found")
            return []

        # Log summary
        logger.info("Found %d physical camera(s)", len(candidates))
        for phys_id, dev in candidates.items():
            logger.info(
                "Camera candidate: %s (%s), backend=%s, formats=%s, resolutions=%s, ffmpeg=%s",
                dev,
                candidate_names[phys_id],
                candidate_backends.get(phys_id, "unknown"),
                candidate_formats[phys_id],
                candidate_resolutions.get(phys_id, []),
                candidate_ffmpeg_results.get(phys_id, ""),
            )

        # Step 2: Resolve roles using bus + name heuristics
        roles = self._resolve_roles(candidates, candidate_names, candidate_formats)

        # Step 3: Build CameraInfo for each role
        result = []
        if roles.get("internal"):
            phys_id = roles["internal"]
            result.append(CameraInfo(
                device_path=candidates[phys_id],
                camera_type="internal",
                index=0,
                formats=candidate_formats[phys_id],
                name=candidate_names[phys_id],
                resolutions=candidate_resolutions.get(phys_id, []),
                reason=candidate_reasons.get(phys_id, ""),
                backend=candidate_backends.get(phys_id, "v4l2"),
            ))
        if roles.get("external"):
            phys_id = roles["external"]
            result.append(CameraInfo(
                device_path=candidates[phys_id],
                camera_type="external",
                index=1,
                formats=candidate_formats[phys_id],
                name=candidate_names[phys_id],
                resolutions=candidate_resolutions.get(phys_id, []),
                reason=candidate_reasons.get(phys_id, ""),
                backend=candidate_backends.get(phys_id, "v4l2"),
            ))

        logger.info("=== Camera Detection Complete: %d role(s) assigned ===", len(result))
        return result

    def detect_all_valid_cameras(self) -> List[CameraInfo]:
        """
        Detect all valid USB V4L2 camera devices using robust enumeration.

        Strategy:
        1. Enumerate /dev/v4l/by-id/* USB camera symlinks
        2. Filter to capture nodes only (not metadata nodes)
        3. Validate USB bus connection
        4. Probe V4L2 capabilities and formats
        5. Test device accessibility with ffmpeg
        6. Return stable CameraInfo entries

        Returns:
            List of CameraInfo for valid USB cameras
        """
        devices = []
        cand_paths: List[str] = []

        # Method 1: /dev/v4l/by-id (preferred - stable USB serials)
        try:
            by_id_dir = Path('/dev/v4l/by-id')
            if by_id_dir.exists():
                logger.debug("Enumerating USB cameras from /dev/v4l/by-id")
                for p in sorted(by_id_dir.iterdir()):
                    try:
                        if not p.is_symlink():
                            continue

                        name = p.name
                        # Only consider USB video devices
                        if not (name.startswith('usb-') and 'video-index' in name):
                            continue

                        # Resolve symlink and validate target exists
                        try:
                            target = p.resolve()
                            if target.exists() and str(target).startswith('/dev/video'):
                                # Double-check the target device actually exists and is accessible
                                if os.path.exists(str(target)):
                                    # Filter out metadata nodes
                                    if not self._is_metadata_node(str(target)):
                                        cand_paths.append(str(p))
                                        logger.debug("Found valid USB camera symlink: %s -> %s", name, target)
                                    else:
                                        logger.debug("Skipping metadata node symlink: %s -> %s", name, target)
                                else:
                                    logger.debug("Skipping symlink with non-existent target: %s -> %s", name, target)
                            else:
                                logger.debug("Skipping symlink with invalid target: %s -> %s", name, target)
                        except (OSError, RuntimeError) as e:
                            logger.debug("Error resolving symlink %s: %s", p.name, e)
                    except Exception as e:
                        logger.debug("Error processing by-id entry %s: %s", p.name, e)
                        continue
        except Exception as e:
            logger.debug("Failed to enumerate /dev/v4l/by-id: %s", e)

        # Method 2: Fallback to by-path
        if not cand_paths:
            try:
                by_path_dir = Path('/dev/v4l/by-path')
                if by_path_dir.exists():
                    logger.debug("Falling back to /dev/v4l/by-path")
                    for p in sorted(by_path_dir.iterdir()):
                        try:
                            if not p.is_symlink():
                                continue

                            name = p.name
                            # Look for USB-related paths (usb- prefix or pci-usb paths)
                            is_usb_path = ('usb-' in name and 'video-index' in name) or ('pci-' in name and 'usb' in name and 'video-index' in name)
                            if not is_usb_path:
                                continue

                            try:
                                target = p.resolve()
                                if target.exists() and str(target).startswith('/dev/video'):
                                    # Double-check the target device actually exists and is accessible
                                    if os.path.exists(str(target)):
                                        if not self._is_metadata_node(str(target)):
                                            cand_paths.append(str(p))
                                            logger.debug("Found valid USB camera via by-path: %s -> %s", name, target)
                                        else:
                                            logger.debug("Skipping metadata node via by-path: %s -> %s", name, target)
                                    else:
                                        logger.debug("Skipping by-path symlink with non-existent target: %s -> %s", name, target)
                                else:
                                    logger.debug("Skipping by-path symlink with invalid target: %s -> %s", name, target)
                            except (OSError, RuntimeError) as e:
                                logger.debug("Error resolving by-path symlink %s: %s", p.name, e)
                        except Exception as e:
                            logger.debug("Error processing by-path entry %s: %s", p.name, e)
                            continue
            except Exception as e:
                logger.debug("Failed to enumerate /dev/v4l/by-path: %s", e)

        # Method 3: Final fallback to direct enumeration
        if not cand_paths:
            logger.debug("Final fallback to direct /dev/video* enumeration")
            try:
                for device_path in sorted(glob('/dev/video*')):
                    try:
                        # Filter out metadata nodes
                        if self._is_metadata_node(device_path):
                            continue

                        # Validate USB bus
                        bus = self._get_device_bus(device_path)
                        if bus == 'usb':
                            cand_paths.append(device_path)
                            logger.debug("Found USB device via direct scan: %s", device_path)
                    except Exception as e:
                        logger.debug("Error checking device %s: %s", device_path, e)
                        continue
            except Exception as e:
                logger.debug("Failed direct video enumeration: %s", e)

        logger.info("Probing %d candidate USB camera devices", len(cand_paths))

        for idx, path in enumerate(cand_paths):
            try:
                # Resolve symlink to actual device if it's a symlink
                if Path(path).is_symlink():
                    resolved = str(Path(path).resolve())
                else:
                    resolved = path

                # CRITICAL: Skip devices that are currently locked/streaming
                # This prevents conflicts with active FFmpeg processes
                from .device_lock import manager as device_lock_manager
                if device_lock_manager.is_locked(resolved):
                    logger.debug("Skipping locked device (actively streaming): %s", resolved)
                    continue

                name = self._get_device_name(resolved)

                # Confirm USB bus connection (multiple validation methods)
                is_usb = self._confirm_usb_device(resolved)
                if not is_usb:
                    logger.info('Skipping non-USB device: %s (%s)', path, name)
                    continue

                # Probe V4L2 formats and capabilities
                formats = self._probe_formats(resolved)

                # Test device accessibility with ffmpeg (critical validation)
                can_open, ffmpeg_reason, _rank = self._ffmpeg_can_open(resolved, formats)
                logger.info('Device %s (%s) probe: can_open=%s reason=%s', path, name, can_open, ffmpeg_reason)

                if not can_open:
                    logger.info('Skipping invalid device: %s (%s): %s', path, name, ffmpeg_reason)
                    continue

                # Get supported resolutions
                resolutions = self._probe_resolutions(resolved)

                # Create CameraInfo with stable path (use symlink path for by-id/by-path)
                ci = CameraInfo(
                    device_path=path,  # Use symlink path for stability
                    camera_type='usb',
                    index=idx,
                    formats=formats,
                    name=name,
                    resolutions=resolutions,
                    reason=ffmpeg_reason,
                    backend='v4l2',
                )
                devices.append(ci)
                logger.info('Added valid USB camera: %s (%s) formats=%s', path, name, formats)

            except Exception as e:
                logger.exception('Error processing candidate device %s: %s', path, e)
                continue

        logger.info('detect_all_valid_cameras found %d valid USB camera devices', len(devices))
        return devices

    def _confirm_usb_device(self, device_path: str) -> bool:
        """
        Confirm device is USB-connected using multiple validation methods.

        Args:
            device_path: Device path to check (may be symlink)

        Returns:
            True if confirmed USB device
        """
        try:
            # If this is a symlink, resolve it for property checking
            actual_device = device_path
            if os.path.islink(device_path):
                try:
                    actual_device = os.path.realpath(device_path)
                except:
                    pass

            # Method 1: udev properties on actual device
            props = self._get_udev_properties(actual_device)
            if props and props.get('ID_BUS', '').lower() == 'usb':
                return True

            # Method 2: sysfs USB parent traversal on actual device
            bus = self._get_device_bus(actual_device)
            if bus == 'usb':
                return True

            # Method 3: Check symlink name for USB indicators
            basename = os.path.basename(device_path)
            if 'usb-' in basename or ('pci-' in basename and 'usb' in basename):
                # Additional validation via udev on symlink
                symlink_props = self._get_udev_properties(device_path)
                if symlink_props and symlink_props.get('ID_BUS', '').lower() == 'usb':
                    return True
                # Fallback: assume USB if symlink name indicates it
                return True

        except Exception as e:
            logger.debug('Error confirming USB status for %s: %s', device_path, e)

        return False

    def _get_v4l2_capabilities(self, device_path: str) -> str:
        """Get V4L2 device capabilities string."""
        try:
            result = subprocess.run(
                ['v4l2-ctl', '--device', device_path, '--info'],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                return result.stdout.lower()
        except Exception:
            pass
        return ""

    def _is_metadata_node(self, device_path: str) -> bool:
        """
        Check if device path represents a metadata-only node.

        USB cameras expose multiple nodes:
        - video-index0: Capture node
        - video-index1: Metadata node (skip)
        """
        try:
            # Check udev capabilities
            props = self._get_udev_properties(device_path)
            if props:
                caps = props.get('ID_V4L_CAPABILITIES', '')
                if ':metadata:' in caps and ':capture:' not in caps:
                    return True

            # Check device name pattern for metadata indicators
            device_name = Path(device_path).name
            if 'metadata' in device_name.lower():
                return True

            # Heuristic: odd-numbered video devices are often metadata
            match = re.search(r'video(\d+)$', device_name)
            if match:
                index = int(match.group(1))
                if index > 0 and index % 2 == 1:
                    # Verify paired even device exists
                    even_device = f'/dev/video{index - 1}'
                    if os.path.exists(even_device):
                        return True

        except Exception as e:
            logger.debug('Error checking metadata status for %s: %s', device_path, e)

        return False

    def get_internal_camera(self) -> Optional[CameraInfo]:
        for cam in self.detect_cameras():
            if cam.camera_type == "internal":
                return cam
        return None

    def get_external_camera(self) -> Optional[CameraInfo]:
        for cam in self.detect_cameras():
            if cam.camera_type == "external":
                return cam
        return None

    def get_cameras_for_streaming(self) -> dict[str, CameraInfo]:
        return {cam.camera_type: cam for cam in self.detect_cameras()}


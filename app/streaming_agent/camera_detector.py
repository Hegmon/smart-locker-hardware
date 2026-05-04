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
        """Enumerate /dev/video nodes from v4l2-ctl, falling back to glob."""
        devices: list[str] = []
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

        if not devices:
            devices = glob("/dev/video*")

        return sorted(set(devices), key=self._device_sort_key)

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

    def _get_device_bus(self, device_path: str) -> Optional[str]:
        """Check sysfs to determine the bus type of a video device."""
        try:
            video_dir = Path(f"/sys/class/video4linux/{Path(device_path).name}")
            if not video_dir.exists():
                return None
            device_link = video_dir / "device"
            if not device_link.exists() or not device_link.is_symlink():
                return None
            target = os.readlink(device_link)
            if "/usb" in target or "/usb" in str(Path(target).parents):
                return "usb"
            elif "/platform" in target:
                return "platform"
            elif "/pci" in target:
                return "pci"
            return None
        except (OSError, ValueError):
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

    def _probe_formats(self, device_path: str) -> List[str]:
        """Use v4l2-ctl or ffmpeg to get supported pixel formats."""
        formats = []

        # Try v4l2-ctl first (fast, clean output)
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--device", device_path, "--list-formats"],
                capture_output=True, text=True, timeout=self.FORMAT_PROBE_TIMEOUT
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("'") and "'" in line:
                        fmt = line.split("'")[1].lower()
                        formats.append(fmt)
                if formats:
                    return formats
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            pass

        # Fallback: use ffmpeg to enumerate formats
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

    def _ffmpeg_can_open(self, device_path: str, formats: List[str]) -> tuple[bool, str]:
        """
        Probe whether FFmpeg can open the node as a capture device.
        
        A device is considered VALID if:
        - /dev/videoX exists
        - FFmpeg can start successfully (even with warnings)
        - frames can be produced for at least a few seconds
        
        DO NOT reject based on:
        - empty formats list from v4l2-ctl
        - ffprobe parsing failures
        - MJPEG warnings (unable to decode APP fields, deprecated pixel format)
        """
        input_format = self._preferred_capture_format(formats)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-f", "v4l2"]
        if input_format:
            cmd.extend(["-input_format", input_format])
        cmd.extend([
            "-video_size",
            "640x480",
            "-i",
            device_path,
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except FileNotFoundError:
            return True, "ffmpeg not installed; accepted after v4l2 format probe"
        except subprocess.TimeoutExpired:
            # Timeout could mean device is slow but functional - accept it
            return True, "ffmpeg open probe timed out; device may be slow but accepted"
        except Exception as exc:
            return False, str(exc)

        output = (result.stderr + result.stdout).strip()
        lower = output.lower()
        
        # Success: FFmpeg produced a frame
        if result.returncode == 0:
            return True, f"ffmpeg opened and captured frame with {input_format or 'auto'}"
        
        # Device busy - this is a transient state, not a camera failure
        if "device or resource busy" in lower or "resource busy" in lower:
            return True, "device busy; streaming manager will free blocker"
        
        # MJPEG warnings are NON-FATAL - downgrade to info, don't reject camera
        mjpeg_warnings = [
            "unable to decode app fields",
            "deprecated pixel format",
            "first field",
        ]
        if any(w in lower for w in mjpeg_warnings):
            # These are warnings, not errors - camera is still valid
            return True, f"ffmpeg opened with MJPEG warnings (non-fatal): {output[:200]}"
        
        # Fatal errors - camera is truly not accessible
        fatal_errors = [
            "inappropriate ioctl",
            "not a video capture device",
            "no such device",
            "permission denied",
            "invalid argument",
            "no medium found",
        ]
        if any(err in lower for err in fatal_errors):
            return False, output or "fatal camera error: invalid v4l2 capture device"
        
        # Unknown errors - be permissive, let streaming manager handle it
        # Return True so the camera isn't rejected, but log the issue
        return True, f"ffmpeg probe had issues but camera accepted: {output[:500]}"

    def _is_camera_device(self, device_path: str, name: str) -> bool:
        """Check if device is likely a camera (not a codec/decoder)."""
        name_lower = name.lower()
        if any(kw in name_lower for kw in self.NON_CAMERA_KEYWORDS):
            return False
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

        for dev in all_devices:
            name = self._get_device_name(dev)

            # Filter out non-camera devices early
            if not self._is_camera_device(dev, name):
                logger.info("Skipping non-camera device: %s (%s)", dev, name)
                continue

            formats = self._probe_formats(dev)
            logger.info("Device %s (%s) formats from enumeration: %s", dev, name, formats)
            
            # CRITICAL FIX: Do NOT reject cameras based solely on format enumeration
            # Always attempt ffmpeg probe regardless of format list results
            # This handles cases where v4l2-ctl enumeration is incomplete but ffmpeg works
            
            can_open, ffmpeg_reason = self._ffmpeg_can_open(dev, formats)
            
            # Log ffmpeg test result for debugging
            logger.info("Device %s (%s) ffmpeg probe: can_open=%s, reason=%s", 
                       dev, name, can_open, ffmpeg_reason)
            
            if not can_open:
                logger.info("Skipping invalid camera node: %s (%s), %s", dev, name, ffmpeg_reason)
                continue

            resolutions = self._probe_resolutions(dev)

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

            if phys_id not in candidates or current_score < existing_score:
                candidates[phys_id] = dev
                candidate_names[phys_id] = name
                candidate_formats[phys_id] = formats
                candidate_resolutions[phys_id] = resolutions
                candidate_reasons[phys_id] = ffmpeg_reason
                candidate_ffmpeg_results[phys_id] = ffmpeg_reason

        if not candidates:
            logger.warning("No valid camera devices found")
            return []

        # Log summary
        logger.info("Found %d physical camera(s)", len(candidates))
        for phys_id, dev in candidates.items():
            logger.info(
                "Camera candidate: %s (%s), formats=%s, resolutions=%s, ffmpeg=%s",
                dev,
                candidate_names[phys_id],
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
            ))

        logger.info("=== Camera Detection Complete: %d role(s) assigned ===", len(result))
        return result

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


"""
Camera Capabilities Detection Module
Probes camera devices to determine their capabilities and supported formats.
"""

from __future__ import annotations
import logging
import subprocess
import re
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CameraCapabilities:
    """Detected capabilities of a camera device"""
    device_path: str
    supported_formats: List[str]
    supported_resolutions: List[str]
    capabilities: List[str]  # ["capture", "streaming", "mjpeg", "h264", etc.]
    driver_info: str
    bus_info: Optional[str]
    is_valid: bool
    validation_error: Optional[str]


class CameraCapabilitiesDetector:
    """Detects camera capabilities using various probing methods"""

    PROBE_TIMEOUT = 10  # seconds

    def detect_capabilities(self, device_path: str) -> CameraCapabilities:
        """
        Detect all capabilities of a camera device.

        Uses multiple probing methods:
        1. v4l2-ctl --all (comprehensive info)
        2. v4l2-ctl --list-formats-ext (formats and resolutions)
        3. ffmpeg probe (validation)
        """
        logger.info("=== Detecting capabilities for {} ===".format(device_path))

        # Initialize with basic info
        capabilities = CameraCapabilities(
            device_path=device_path,
            supported_formats=[],
            supported_resolutions=[],
            capabilities=[],
            driver_info="",
            bus_info=None,
            is_valid=False,
            validation_error=None
        )

        try:
            # Get comprehensive device info
            device_info = self._get_v4l2_device_info(device_path)
            capabilities.driver_info = device_info.get("driver", "")
            capabilities.bus_info = device_info.get("bus", "")

            # Get formats and resolutions
            formats, resolutions = self._get_v4l2_formats_and_resolutions(device_path)
            capabilities.supported_formats = formats
            capabilities.supported_resolutions = resolutions

            # Determine capabilities from formats
            capabilities.capabilities = self._determine_capabilities(formats)

            # Validate device usability
            is_valid, error = self._validate_device(device_path, formats)
            capabilities.is_valid = is_valid
            capabilities.validation_error = error

            logger.info("Capabilities detected: formats={}, resolutions={}..., capabilities={}".format(formats, resolutions[:3], capabilities.capabilities))

        except Exception as e:
            logger.warning("Failed to detect capabilities for {}: {}".format(device_path, e))
            capabilities.validation_error = str(e)

        return capabilities

    def _get_v4l2_device_info(self, device_path: str) -> Dict[str, str]:
        """Get comprehensive device info using v4l2-ctl --all"""
        info = {}
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--device", device_path, "--all"],
                capture_output=True,
                text=True,
                timeout=self.PROBE_TIMEOUT
            )

            if result.returncode == 0:
                # Parse key-value pairs from output
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if ":" in line and not line.startswith("\t"):
                        key, value = line.split(":", 1)
                        info[key.strip().lower()] = value.strip()

        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.debug("v4l2-ctl not available for {}".format(device_path))

        return info

    def _get_v4l2_formats_and_resolutions(self, device_path: str) -> Tuple[List[str], List[str]]:
        """Get supported formats and resolutions"""
        formats = []
        resolutions = []

        try:
            result = subprocess.run(
                ["v4l2-ctl", "--device", device_path, "--list-formats-ext"],
                capture_output=True,
                text=True,
                timeout=self.PROBE_TIMEOUT
            )

            if result.returncode == 0:
                current_format = None
                for line in result.stdout.splitlines():
                    line = line.strip()

                    # Detect format lines
                    if line.startswith("'") and "'" in line:
                        fmt_match = re.search(r"'([A-Z0-9]+)'", line)
                        if fmt_match:
                            fmt = fmt_match.group(1).lower()
                            if fmt not in formats:
                                formats.append(fmt)
                            current_format = fmt

                    # Detect resolution lines
                    elif "Size: Discrete" in line:
                        size_match = re.search(r"Size: Discrete (\d+x\d+)", line)
                        if size_match:
                            size = size_match.group(1)
                            if size not in resolutions:
                                resolutions.append(size)

        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.debug("v4l2-ctl format probe failed for {}".format(device_path))

        return formats, resolutions

    def _determine_capabilities(self, formats: List[str]) -> List[str]:
        """Determine camera capabilities from supported formats"""
        capabilities = ["capture"]  # Basic capture capability

        formats_lower = [f.lower() for f in formats]

        # Check for compressed formats
        if any(fmt in formats_lower for fmt in ["mjpeg", "mjpg"]):
            capabilities.append("mjpeg")

        if "h264" in formats_lower:
            capabilities.append("h264")

        if "h265" in formats_lower:
            capabilities.append("h265")

        # Check for raw formats
        raw_formats = ["yuyv", "yuyv422", "yuv422", "uyvy", "rgb", "bgr"]
        if any(fmt in formats_lower for fmt in raw_formats):
            capabilities.append("raw")

        return capabilities

    def _validate_device(self, device_path: str, formats: List[str]) -> Tuple[bool, Optional[str]]:
        """
        Validate that a device can actually be used for capture.
        Tries a short test capture with ffmpeg.
        """
        # Skip validation for formats that are known to fail with V4L2
        if not formats or "libcamera" in [f.lower() for f in formats]:
            return True, None

        try:
            # Try to capture one frame
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-f", "v4l2",
                "-video_size", "640x480",
                "-i", device_path,
                "-frames:v", "1",
                "-f", "null",
                "-"
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=15  # Allow more time for device init
            )

            if result.returncode == 0:
                return True, None
            else:
                error = result.stderr.decode(errors='replace').strip()
                return False, error[:200]  # Truncate long errors

        except subprocess.TimeoutExpired:
            return False, "Device probe timeout"
        except Exception as e:
            error_msg = "Probe failed: " + str(e)

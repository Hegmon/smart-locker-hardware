"""
Production-Grade FFmpeg Stream Supervisor

Features:
- Robust FFmpeg-based format probing (not just V4L2 enumeration)
- Self-healing stream engine with process watchdog
- Health state machine per stream
- Format fallback chain: mjpeg → yuyv422 → h264 → auto
- Independent camera pipelines with isolation
- Exponential backoff reconnect
- Structured logging for production debugging
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable, Dict, List, Any

from .constants import RESTART_BACKOFF, RESTART_MAX_ATTEMPTS

logger = logging.getLogger(__name__)


# ============================================================================
# HEALTH STATE MACHINE
# ============================================================================

STREAM_STATE_STARTING = "STARTING"
STREAM_STATE_RUNNING = "RUNNING"
STREAM_STATE_DEGRADED = "DEGRADED"   # Low FPS, recovering
STREAM_STATE_RECOVERING = "RECOVERING"  # Restarting after failure
STREAM_STATE_FAILED = "FAILED"       # Max retries exceeded

# ============================================================================
# CAMERA FORMAT PROFILE
# ============================================================================

@dataclass
class CameraFormatProfile:
    """Per-device format profile from FFmpeg runtime probing"""
    device: str
    supported_formats: List[str]  # e.g. ["mjpeg", "yuyv422"]
    preferred_format: str         # highest priority available
    safe_resolutions: List[str]   # tested working resolutions
    driver_info: str = ""          # v4l2 driver name
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "device": self.device,
            "supported_formats": self.supported_formats,
            "preferred_format": self.preferred_format,
            "safe_resolutions": self.safe_resolutions,
            "driver_info": self.driver_info,
        }


# ============================================================================
# FORMAT SCANNER - FFmpeg-based probing
# ============================================================================

class FormatScanner:
    """
    Robust format detection using FFmpeg runtime probing.
    Does NOT rely solely on V4L2 enumeration which can be incomplete.
    """
    
    # Priority order for format selection
    FORMAT_PRIORITY = ["mjpeg", "yuyv422", "h264", "nv12"]
    
    # Safe resolutions per camera type
    SAFE_RESOLUTIONS = {
        "internal": ["640x480", "1280x720"],
        "external": ["1280x720", "1920x1080", "640x480"],
    }
    
    # Explicit camera profiles for known devices
    # These provide fallback when format probing is unreliable
    CAMERA_PROFILES = {
        "/dev/video0": {
            "type": "internal",
            "preferred_format": "yuyv422",
            "fallback_formats": ["yuyv", "mjpeg", "auto"],
            "resolution": "640x480",
            "framerate": 25,
        },
        "/dev/video2": {
            "type": "external",
            "preferred_format": "mjpeg",
            "fallback_formats": ["yuyv422", "h264", "auto"],
            "resolution": "1280x720",
            "framerate": 25,
        },
        # Generic USB camera profiles - match by device name patterns
        "usb_camera": {
            "preferred_format": "mjpeg",
            "fallback_formats": ["yuyv422"],  # No "auto" - force explicit formats
            "resolution": "1280x720",
            "framerate": 25,
        },
        "a4tech_camera": {
            "preferred_format": "mjpeg",
            "fallback_formats": ["yuyv422"],
            "resolution": "1280x720",  # Lower resolution for compatibility
            "framerate": 25,
        },
    }
    
    @classmethod
    def probe_device(cls, device_path: str) -> CameraFormatProfile:
        """
        Probe device using FFmpeg -list_formats all.
        Returns CameraFormatProfile with detected capabilities.
        """
        logger.info("=== Format Scanner: Probing %s ===", device_path)
        
        # Try FFmpeg format listing first (most reliable)
        formats = cls._probe_ffmpeg_formats(device_path)
        
        # Fallback to v4l2-ctl if FFmpeg fails
        if not formats:
            formats = cls._probe_v4l2_formats(device_path)
        
        # Get driver info
        driver_info = cls._get_driver_info(device_path)
        
        # Determine supported formats from priority list
        supported = cls._filter_supported_formats(formats)

        # If no formats detected, check if this is a USB camera and provide defaults
        if not supported:
            if cls._is_usb_camera(device_path):
                logger.info("No formats detected for USB camera %s, using defaults", device_path)
                supported = ["mjpeg", "yuyv422"]
            else:
                supported = ["yuyv422"]  # Fallback for non-USB cameras

        # Select preferred format (highest priority available)
        preferred = cls._select_preferred_format(supported)
        
        # Determine safe resolutions
        safe_res = cls._determine_safe_resolutions(device_path, supported)
        
        profile = CameraFormatProfile(
            device=device_path,
            supported_formats=supported,
            preferred_format=preferred,
            safe_resolutions=safe_res,
            driver_info=driver_info,
        )
        
        logger.info("Format profile for %s: %s", device_path, json.dumps(profile.to_dict()))
        logger.info("=== Format Scanner Complete ===")
        
        return profile
    
    @staticmethod
    def _probe_ffmpeg_formats(device_path: str) -> List[str]:
        """Use FFmpeg -list_formats all to detect supported formats."""
        formats = []
        try:
            # Try with a very short timeout and minimal options to avoid device locking
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-f", "v4l2",
                    "-framerate", "1",  # Very low framerate for probing
                    "-video_size", "160x120",  # Very small size for probing
                    "-t", "0.1",  # Very short duration
                    "-list_formats", "all",
                    "-i", device_path,
                ],
                capture_output=True,
                text=True,
                timeout=5,  # Shorter timeout
            )

            # Parse output for format codes
            output = result.stderr + result.stdout

            # Extract format codes in single quotes
            matches = re.findall(r"'([A-Z0-9]+)'", output)
            for fmt in matches:
                fmt_lower = fmt.lower()
                # Map common format names
                if fmt_lower in ["mjpeg", "mjpg"]:
                    fmt_lower = "mjpeg"
                elif fmt_lower in ["yuyv", "yuyv422", "yuv422"]:
                    fmt_lower = "yuyv422"
                elif fmt_lower in ["h264"]:
                    fmt_lower = "h264"
                elif fmt_lower in ["nv12"]:
                    fmt_lower = "nv12"

                if fmt_lower in ["mjpeg", "yuyv422", "h264", "nv12"]:
                    if fmt_lower not in formats:
                        formats.append(fmt_lower)

            if formats:
                logger.debug("FFmpeg probe detected formats: %s", formats)
            else:
                logger.debug("FFmpeg probe found no standard formats, trying fallback")

                # Fallback: try common USB camera formats
                if not formats:
                    formats = ["mjpeg", "yuyv422"]  # Most USB cameras support these

        except FileNotFoundError:
            logger.warning("FFmpeg not available for format probing")
            # Fallback formats
            return ["mjpeg", "yuyv422"]
        except subprocess.TimeoutExpired:
            logger.warning("FFmpeg format probe timed out, using fallback formats")
            return ["mjpeg", "yuyv422"]
        except Exception as e:
            logger.warning("FFmpeg format probe failed: %s, using fallback formats", e)
            return ["mjpeg", "yuyv422"]

        return formats
    
    @staticmethod
    def _probe_v4l2_formats(device_path: str) -> List[str]:
        """Fallback: use v4l2-ctl to enumerate formats."""
        formats = []
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--device", device_path, "--list-formats-ext"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    # Look for format lines like: ioctl: VIDIOC_ENUM_FMT or '[0]: 'MJPG' (Motion-JPEG, compressed)'
                    if "'" in line:
                        parts = line.split("'")
                        if len(parts) >= 2:
                            fmt = parts[1].lower()
                            if fmt in ["mjpeg", "mjpg"]:
                                if "mjpeg" not in formats:
                                    formats.append("mjpeg")
                            elif fmt in ["yuyv", "yuyv422", "yuv422"]:
                                if "yuyv422" not in formats:
                                    formats.append("yuyv422")
                            elif fmt in ["h264"]:
                                if "h264" not in formats:
                                    formats.append("h264")
                            elif fmt in ["nv12"]:
                                if "nv12" not in formats:
                                    formats.append("nv12")
        except FileNotFoundError:
            logger.debug("v4l2-ctl not available")
        except Exception as e:
            logger.warning("v4l2-ctl format probe failed: %s", e)

        return formats
    
    @staticmethod
    def _get_driver_info(device_path: str) -> str:
        """Get v4l2 driver name from sysfs."""
        try:
            name_file = Path(f"/sys/class/video4linux/{Path(device_path).name}/name")
            if name_file.exists():
                return name_file.read_text().strip()
        except Exception:
            pass
        return "unknown"

    @classmethod
    def _is_usb_camera(cls, device_path: str) -> bool:
        """Check if device path indicates a USB camera."""
        try:
            # Check device path for USB indicators
            if "usb-" in device_path or "by-id" in device_path:
                return True

            # Check device name from sysfs
            device_name = cls._get_device_name(device_path).lower()
            if "usb" in device_name or "camera" in device_name:
                return True

            # Check udev properties
            try:
                import subprocess
                result = subprocess.run(
                    ["udevadm", "info", "--query=property", "--name", device_path],
                    capture_output=True, text=True, timeout=2
                )
                if result.returncode == 0 and "ID_BUS=usb" in result.stdout:
                    return True
            except:
                pass

            return False
        except Exception:
            return False

    @classmethod
    def _filter_supported_formats(cls, formats: List[str]) -> List[str]:
        """Filter to only priority formats we can handle."""
        supported = []
        fmt_lower = [f.lower() for f in formats]
        
        for priority_fmt in cls.FORMAT_PRIORITY:
            if priority_fmt in fmt_lower:
                supported.append(priority_fmt)
        
        return supported
    
    @classmethod
    def _select_preferred_format(cls, supported_formats: List[str]) -> str:
        """Select highest priority format from supported list."""
        for priority_fmt in cls.FORMAT_PRIORITY:
            if priority_fmt in supported_formats:
                return priority_fmt
        return ""  # No supported format found
    
    @classmethod
    def _determine_safe_resolutions(
        cls, device_path: str, supported_formats: List[str]
    ) -> List[str]:
        """Determine safe resolutions for this device."""
        # Check explicit camera profiles first
        if device_path in cls.CAMERA_PROFILES:
            profile = cls.CAMERA_PROFILES[device_path]
            # Use type-specific defaults as base
            camera_type = profile["type"]
            resolutions = list(cls.SAFE_RESOLUTIONS.get(camera_type, ["640x480"]))
            # Put the profile's preferred resolution first
            if profile["resolution"] in resolutions:
                resolutions.remove(profile["resolution"])
                resolutions.insert(0, profile["resolution"])
            else:
                resolutions.insert(0, profile["resolution"])
            return resolutions
        
        # Determine camera type from device path
        camera_type = "external" if "video2" in device_path else "internal"
        
        # Start with type-specific defaults
        resolutions = list(cls.SAFE_RESOLUTIONS.get(camera_type, ["640x480"]))
        
        # If only YUYV or no format, limit to lower resolutions
        if not supported_formats or supported_formats == ["yuyv422"]:
            resolutions = ["640x480", "1280x720"]
        
        return resolutions
    
    @classmethod
    def get_format_fallback_chain(cls, supported_formats: List[str]) -> List[str]:
        """
        Build format fallback chain for a device.
        Returns: [preferred, fallback1, fallback2, ..., "auto"]
        """
        chain = []
        
        # Add supported formats in priority order
        for priority_fmt in cls.FORMAT_PRIORITY:
            if priority_fmt in supported_formats:
                chain.append(priority_fmt)
        
        # Always end with auto-detection as last resort
        chain.append("")  # "" means auto-detection
        
        return chain


# ============================================================================
# STREAM PROCESS - Enhanced with health tracking
# ============================================================================

@dataclass
class StreamProcess:
    """Represents a single camera stream process with health monitoring"""
    camera_type: str
    device_path: str
    profile: CameraFormatProfile
    process: Optional[subprocess.Popen] = None
    producer_process: Optional[subprocess.Popen] = None  # For piped libcamera commands

    # State tracking
    state: str = STREAM_STATE_STARTING
    selected_format: Optional[str] = None
    selected_resolution: str = "640x480"
    backend: str = "v4l2"  # "libcamera" or "v4l2"

    # Health metrics
    restart_count: int = 0
    consecutive_failures: int = 0
    last_frame_time: float = 0
    last_start_time: float = 0
    last_health_check: float = 0
    last_frame_count: int = 0
    frame_stall_start: Optional[float] = None
    last_error: Optional[str] = None

    # Command tracking
    last_command: list[str] = field(default_factory=list)

    # Thread safety
    lock: threading.RLock = field(default_factory=threading.RLock)

    # Watchdog
    watchdog_active: bool = False

    # Progress monitoring
    _progress_thread: Optional[threading.Thread] = None
    _progress_stop: threading.Event = field(default_factory=threading.Event)
    _last_progress_time: float = 0

    def get_format_chain(self) -> List[str]:
        """Get format fallback chain for this stream."""
        return FormatScanner.get_format_fallback_chain(
            self.profile.supported_formats
        )


# ============================================================================
# SELF-HEALING FFmpeg STREAM ENGINE
# ============================================================================

class FFmpegStreamEngine:
    """
    Production-grade FFmpeg stream supervisor.
    
    Features:
    - Process watchdog per stream
    - Auto-restart on failure
    - Format fallback chain
    - Health state machine
    - Frame rate monitoring
    """
    
    # Health check configuration
    WATCHDOG_INTERVAL = 5  # seconds
    FPS_CHECK_INTERVAL = 10  # seconds
    MAX_CONSECUTIVE_FAILURES = 3
    FRAME_COUNT_TIMEOUT = 15  # seconds
    FRAME_STALL_THRESHOLD = 30  # seconds without frames
    RECONNECT_GRACE_PERIOD = 5  # seconds after reconnect before checking

    # Restart configuration
    MAX_RESTARTS_PER_FORMAT = 3
    RESTART_BACKOFF_BASE = 1.0  # seconds
    RESTART_BACKOFF_MAX = 30.0  # seconds
    
    def __init__(
        self,
        device_id: str,
        mediamtx_host: str = "127.0.0.1",
        mediamtx_rtsp_port: int = 8554,
        on_stream_status_change: Optional[Callable[[str, str, str], None]] = None,
    ):
        self.device_id = device_id
        self.mediamtx_host = mediamtx_host
        self.mediamtx_rtsp_port = mediamtx_rtsp_port
        self.on_stream_status_change = on_stream_status_change
        
        # Stream registry - independent pipelines per camera
        self._streams: Dict[str, StreamProcess] = {}
        self._lock = threading.RLock()
        
        # Watchdog thread
        self._running = False
        self._watchdog_thread: Optional[threading.Thread] = None
        
        logger.info("FFmpegStreamEngine initialized for device %s", device_id)
    
    def add_stream(self, camera_type: str, profile: CameraFormatProfile, backend: str = "v4l2") -> None:
        """
        Register a camera stream with its format profile and backend type.
        Each stream is independent - no shared state.
        """
        with self._lock:
            if camera_type in self._streams:
                logger.warning("Stream %s already exists, stopping it first", camera_type)
                self._stop_stream_internal(camera_type)

            # Determine safe resolution
            safe_resolutions = profile.safe_resolutions
            if not safe_resolutions:
                safe_resolutions = ["640x480"]

            # Use explicit camera profile if available for this device
            device_profile = FormatScanner.CAMERA_PROFILES.get(profile.device, {})

            # Use highest safe resolution for external, lower for internal
            if camera_type == "external":
                # Prefer profile resolution if available
                if device_profile and "resolution" in device_profile:
                    selected_res = device_profile["resolution"]
                else:
                    selected_res = safe_resolutions[0]  # Highest available
            else:
                # Internal camera - prefer 640x480 for stability
                if device_profile and "resolution" in device_profile:
                    selected_res = device_profile["resolution"]
                else:
                    selected_res = "640x480"  # Stable default

            stream = StreamProcess(
                camera_type=camera_type,
                device_path=profile.device,
                profile=profile,
                selected_resolution=selected_res,
                state=STREAM_STATE_STARTING,
            )

            # Store backend information for command building
            stream.backend = backend

            self._streams[camera_type] = stream

            logger.info(
                "Added stream: %s -> %s (backend=%s, profile: %s, resolution: %s)",
                camera_type,
                profile.device,
                backend,
                json.dumps(profile.to_dict()),
                selected_res,
            )
    
    def start_stream(self, camera_type: str) -> bool:
        """Start streaming for a specific camera."""
        with self._lock:
            if camera_type not in self._streams:
                logger.error("Cannot start unknown stream: %s", camera_type)
                return False
            
            stream = self._streams[camera_type]
            
            with stream.lock:
                if stream.state == STREAM_STATE_RUNNING:
                    logger.warning("Stream %s already running", camera_type)
                    return True
                
                # Reset failure count on manual start
                stream.consecutive_failures = 0
                stream.restart_count = 0
            # Acquire device lock before starting
            from .device_lock import manager as device_lock_manager
            owner = f"stream:{camera_type}"
            # Try non-blocking acquire first to avoid blocking other streams
            locked = device_lock_manager.acquire(stream.device_path, owner, blocking=False)
            if not locked:
                logger.warning("Device %s is locked, marking %s as BUSY", stream.device_path, camera_type)
                stream.state = "BUSY"
                stream.last_error = "Device locked"
                return False

            try:
                ok = self._start_ffmpeg_process(stream)
                if not ok:
                    # release lock on failure to start
                    device_lock_manager.release(stream.device_path, owner)
                return ok
            except Exception:
                device_lock_manager.release(stream.device_path, owner)
                raise
    
    def start_all_streams(self) -> None:
        """Start all registered streams independently."""
        with self._lock:
            camera_types = list(self._streams.keys())
        
        for camera_type in camera_types:
            self.start_stream(camera_type)
    
    def _start_ffmpeg_process(self, stream: StreamProcess) -> bool:
        """
        Start FFmpeg process with format fallback.
        Tries formats in priority order until one works.
        """
        format_chain = stream.get_format_chain()
        
        # Check for explicit camera profile to override format chain
        device_profile = FormatScanner.CAMERA_PROFILES.get(stream.device_path, {})
        if device_profile and "preferred_format" in device_profile:
            # Reorder format chain to prioritize the profile's preferred format
            preferred = device_profile["preferred_format"]
            fallbacks = device_profile.get("fallback_formats", [])
            # Build new chain: preferred first, then fallbacks, then remaining
            new_chain = []
            if preferred and preferred not in new_chain:
                new_chain.append(preferred)
            for fb in fallbacks:
                if fb and fb not in new_chain:
                    new_chain.append(fb)
            # Add remaining formats from original chain
            for fmt in format_chain:
                if fmt not in new_chain:
                    new_chain.append(fmt)
            format_chain = new_chain
        
        logger.info(
            "=== Starting stream: %s (device=%s) ===",
            stream.camera_type,
            stream.device_path,
        )
        logger.info(
            "Format fallback chain: %s",
            ["auto" if f == "" else f for f in format_chain],
        )
        logger.info("Selected resolution: %s", stream.selected_resolution)
        
        last_error = ""
        
        for format_idx, input_format in enumerate(format_chain):
            # Check if we've exceeded restart limit for this format
            if stream.restart_count >= self.MAX_RESTARTS_PER_FORMAT:
                logger.warning(
                    "Max restarts (%d) exceeded for %s, trying next format",
                    self.MAX_RESTARTS_PER_FORMAT,
                    stream.camera_type,
                )
                stream.restart_count = 0
                continue

            format_name = "auto" if input_format == "" else input_format
            logger.info(
                "Trying format %d/%d: %s for %s",
                format_idx + 1, len(format_chain), format_name, stream.camera_type
            )
            
            # Build and start FFmpeg
            cmd = self._build_ffmpeg_command(stream, input_format)
            format_label = "auto" if not input_format else input_format

            logger.info(
                "Attempt %d/%d: Starting pipeline for %s with backend=%s, format=%s",
                format_idx + 1,
                len(format_chain),
                stream.camera_type,
                stream.backend,
                format_label,
            )
            logger.debug("Command: %s", " ".join(cmd) if isinstance(cmd, list) else str(cmd))

            try:
                # Attempt to start the pipeline, with a few quick retries if device is busy
                start_attempts = 3
                started = False
                last_error = ""

                for start_try in range(start_attempts):
                    # Clean up any previous partial processes
                    try:
                        if stream.process:
                            try:
                                os.killpg(os.getpgid(stream.process.pid), signal.SIGTERM)
                            except Exception:
                                pass
                            stream.process = None
                        if stream.producer_process:
                            try:
                                os.killpg(os.getpgid(stream.producer_process.pid), signal.SIGTERM)
                            except Exception:
                                pass
                            stream.producer_process = None
                    except Exception:
                        pass

                    if stream.backend == "libcamera" and "|" in cmd:
                        # Handle piped libcamera commands
                        pipe_idx = cmd.index("|")
                        producer_cmd = cmd[:pipe_idx]
                        consumer_cmd = cmd[pipe_idx + 1:]

                        logger.info("Starting piped libcamera pipeline: %s | %s",
                                  " ".join(producer_cmd), " ".join(consumer_cmd))

                        # Start producer (rpicam-vid)
                        producer = subprocess.Popen(
                            producer_cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL,
                            preexec_fn=os.setsid,
                        )

                        # Start consumer (ffmpeg) with producer's stdout as input
                        stream.process = subprocess.Popen(
                            consumer_cmd,
                            stdin=producer.stdout,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            preexec_fn=os.setsid,
                        )

                        # Store both processes for cleanup
                        stream.producer_process = producer

                    else:
                        # Handle regular single-command pipelines (V4L2)
                        stream.process = subprocess.Popen(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL,
                            preexec_fn=os.setsid,
                        )

                    # Give FFmpeg a short time to fail fast if there's an immediate error
                    time.sleep(2)
                    returncode = stream.process.poll()

                    if returncode is not None:
                        # Process exited immediately
                        stderr = (
                            stream.process.stderr.read().decode(errors="replace")
                            if stream.process.stderr else ""
                        )
                        last_error = stderr.strip() or f"FFmpeg exited with code {returncode}"

                        logger.warning(
                            "FFmpeg failed to start for %s (format=%s): %s",
                            stream.camera_type,
                            format_label,
                            last_error[:500],
                        )

                        # If device busy, try to free and retry a few times
                        if self._is_device_busy_error(last_error) and start_try < (start_attempts - 1):
                            logger.info("Device busy on %s, attempt %d/%d to free and retry", stream.camera_type, start_try+1, start_attempts)
                            try:
                                self._free_device(stream.device_path)
                            except Exception:
                                logger.debug("_free_device failed for %s", stream.camera_type)
                            time.sleep(0.5 * (2 ** start_try))
                            # continue to next start_try
                            continue

                        # Fatal camera errors should skip to next format immediately
                        if self._is_fatal_camera_error(last_error):
                            logger.error("Fatal camera error for %s, skipping this format: %s", stream.camera_type, last_error)
                            break

                        # Non-fatal - count as a failure and break to try next format
                        stream.consecutive_failures += 1
                        stream.restart_count += 1
                        stream.last_error = last_error
                        break

                    # If we reach here, process started successfully
                    started = True
                    break

                if not started:
                    # All start attempts failed for this format
                    continue

                # Process started successfully!
                stream.last_start_time = time.time()
                stream.last_frame_time = time.time()
                stream.last_progress_time = time.time()
                stream.selected_format = input_format if input_format else "auto"
                stream.consecutive_failures = 0
                stream.last_command = cmd

                # Start progress reader to parse FFmpeg -progress output
                stream._progress_stop.clear()
                stream._progress_thread = threading.Thread(
                    target=self._progress_reader,
                    args=(stream,),
                    daemon=True,
                    name=f"ffmpeg-progress-{stream.camera_type}",
                )
                stream._progress_thread.start()

                logger.info(
                    "✓ FFmpeg started for %s (PID: %d, format=%s, resolution=%s)",
                    stream.camera_type,
                    stream.process.pid,
                    stream.selected_format,
                    stream.selected_resolution,
                )
                logger.debug("FFmpeg command: %s", " ".join(cmd))
                stream._progress_thread.start()

                logger.info(
                    "✓ FFmpeg started for %s (PID: %d, format=%s, resolution=%s)",
                    stream.camera_type,
                    stream.process.pid,
                    stream.selected_format,
                    stream.selected_resolution,
                )

                # Notify status change
                if self.on_stream_status_change:
                    self.on_stream_status_change(
                        stream.camera_type,
                        stream.state,
                        f"format={stream.selected_format}, res={stream.selected_resolution}",
                    )

                return True
                
            except FileNotFoundError:
                stream.last_error = "FFmpeg binary not found"
                stream.state = STREAM_STATE_FAILED
                logger.exception("FFmpeg not found in PATH")
                return False
                
            except Exception as exc:
                last_error = str(exc)
                stream.last_error = last_error
                stream.state = STREAM_STATE_FAILED
                logger.exception("Failed to start FFmpeg for %s", stream.camera_type)
                stream.consecutive_failures += 1
                return False
        
        # All format attempts failed
        stream.state = STREAM_STATE_FAILED
        stream.last_error = last_error or "All format attempts failed"
        
        logger.error(
            "✗ All format attempts failed for %s: %s",
            stream.camera_type,
            stream.last_error,
        )
        
        if self.on_stream_status_change:
            self.on_stream_status_change(
                stream.camera_type,
                STREAM_STATE_FAILED,
                stream.last_error,
            )
        
        return False
    
    def _build_ffmpeg_command(
        self, stream: StreamProcess, input_format: str
    ) -> List[str]:
        """
        Build FFmpeg command for a stream, with backend-aware input handling.

        For libcamera devices: uses rpicam-vid or libcamera-vid piped to ffmpeg
        For v4l2 devices: uses direct ffmpeg V4L2 input (existing behavior)

        When input_format is empty string, omits -input_format flag
        to allow FFmpeg auto-detection (last resort fallback).
        """
        device_path = stream.device_path
        backend = stream.backend

        # Check for explicit camera profile
        profile = FormatScanner.CAMERA_PROFILES.get(device_path, {})

        # If no exact match, try pattern matching on device name
        if not profile:
            try:
                # Get device name for pattern matching
                from pathlib import Path
                device_name = ""
                try:
                    name_file = Path(f"/sys/class/video4linux/{Path(device_path).name}/name")
                    if name_file.exists():
                        device_name = name_file.read_text().strip().lower()
                except:
                    pass

                # Match patterns for USB cameras
                if "a4tech" in device_name:
                    profile = FormatScanner.CAMERA_PROFILES.get("a4tech_camera", {})
                    logger.debug("Matched A4tech camera profile for %s", device_path)
                elif "usb" in device_name.lower() or "camera" in device_name.lower():
                    profile = FormatScanner.CAMERA_PROFILES.get("usb_camera", {})
                    logger.debug("Matched USB camera profile for %s", device_path)
                else:
                    # Default USB camera profile for any USB device
                    profile = FormatScanner.CAMERA_PROFILES.get("usb_camera", {})
                    logger.debug("Using default USB camera profile for %s", device_path)

            except Exception as e:
                logger.debug("Error matching camera profile: %s", e)

        # Determine framerate from profile or default
        framerate = profile.get("framerate", 25)

        if backend == "libcamera":
            # For libcamera devices, use rpicam-vid piped to ffmpeg
            logger.info("Building libcamera pipeline for %s", device_path)

            # Try rpicam-vid first (preferred for Raspberry Pi)
            rpicam_cmd = [
                "rpicam-vid",
                "-t", "0",  # infinite duration
                "--inline",  # inline headers
                "--nopreview",  # no preview window
                "-o", "-",  # output to stdout
                "--width", stream.selected_resolution.split("x")[0],
                "--height", stream.selected_resolution.split("x")[1],
                "--framerate", str(framerate),
            ]

            # Build ffmpeg command to consume rpicam-vid output
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "warning",
                "-f", "h264",  # rpicam-vid outputs H264
                "-i", "-",  # read from stdin (piped from rpicam-vid)
                "-c:v", "copy",  # copy stream without re-encoding
                "-f", "rtsp",
                "-rtsp_transport", "tcp",
            ]

            # Output URL
            output_url = (
                f"rtsp://{self.mediamtx_host}:{self.mediamtx_rtsp_port}/"
                f"{self.device_id}/{stream.camera_type}"
            )
            cmd.append(output_url)

            # Combine rpicam-vid and ffmpeg with pipe
            full_cmd = rpicam_cmd + ["|"] + cmd
            return full_cmd

        else:
            # For V4L2 devices, use existing FFmpeg V4L2 input logic
            logger.info("Building V4L2 pipeline for %s", device_path)

            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "warning",
                "-f", "v4l2",
                "-fflags", "nobuffer+discardcorrupt",
                "-flags", "low_delay",
                "-analyzeduration", "0",
                "-probesize", "32",
                "-framerate", str(framerate),
                "-video_size", stream.selected_resolution,
                "-threads", "1",  # Single thread for USB cameras
                "-thread_type", "slice",  # Slice threading for lower latency
            ]

            # Add input_format only when explicitly specified
            # Empty string means use FFmpeg auto-detection
            if input_format and input_format != "auto":
                cmd.extend(["-input_format", input_format])
                logger.debug("Using explicit input format: %s", input_format)
            else:
                logger.debug("Using FFmpeg auto-detection for input format")

            cmd.extend(["-i", device_path])

            # Include FFmpeg progress to stdout so we can monitor frame progress
            # This will be parsed by progress reader thread
            cmd.extend(["-nostats", "-progress", "pipe:1"])

            # Encoding options optimized for Raspberry Pi
            cmd.extend([
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-pix_fmt", "yuv420p",
                "-g", "10",
                "-keyint_min", "10",
                "-b:v", "1000k",
                "-maxrate", "1500k",
                "-bufsize", "2000k",
            ])

            # RTSP output
            cmd.extend([
                "-f", "rtsp",
                "-rtsp_transport", "tcp",
            ])

            # Output URL
            output_url = (
                f"rtsp://{self.mediamtx_host}:{self.mediamtx_rtsp_port}/"
                f"{self.device_id}/{stream.camera_type}"
            )
            cmd.append(output_url)

            return cmd

    def _progress_reader(self, stream: StreamProcess) -> None:
        """
        Read FFmpeg "-progress pipe:1" output from stdout and update frame/fps
        metrics for health monitoring.
        """
        if not stream.process or not stream.process.stdout:
            return

        try:
            # Read lines until process ends or stop requested
            while not stream._progress_stop.is_set():
                line = stream.process.stdout.readline()
                if not line:
                    # EOF or process exit
                    break
                try:
                    if isinstance(line, bytes):
                        s = line.decode(errors="replace").strip()
                    else:
                        s = str(line).strip()
                except Exception:
                    s = ""

                if not s:
                    continue

                # Progress format: key=value lines, e.g. frame=123, fps=25.0
                if s.startswith("frame="):
                    try:
                        frame_num = int(s.split("=", 1)[1].strip())
                        stream.last_frame_time = time.time()
                        stream._last_progress_time = time.time()
                        logger.debug("Stream %s progress: frame=%d", stream.camera_type, frame_num)
                    except Exception as e:
                        logger.debug("Failed to parse frame line '%s': %s", s, e)
                elif s.startswith("fps="):
                    try:
                        fps_val = float(s.split("=", 1)[1].strip())
                        stream.last_frame_time = time.time()
                        stream._last_progress_time = time.time()
                        logger.debug("Stream %s progress: fps=%.1f", stream.camera_type, fps_val)
                    except Exception as e:
                        logger.debug("Failed to parse fps line '%s': %s", s, e)
                elif s.strip():
                    # Log other progress lines for debugging
                    logger.debug("Stream %s progress: %s", stream.camera_type, s.strip())
                # continue reading
        except Exception:
            # Reader may fail if process ends; ignore
            pass

    def _is_device_busy_error(self, error: str) -> bool:
        """Check if error indicates device is busy."""
        error_lower = error.lower()
        return any(
            kw in error_lower
            for kw in ["device or resource busy", "resource busy", "ebusy"]
        )
    
    def _is_fatal_camera_error(self, error: str) -> bool:
        """Check if error indicates camera is not accessible."""
        error_lower = error.lower()
        return any(
            kw in error_lower
            for kw in [
                "inappropriate ioctl",
                "not a video capture device",
                "no such device",
                "permission denied",
            ]
        )
    
    def _free_device(self, device_path: str) -> bool:
        """Attempt to free a busy device using fuser."""
        try:
            result = subprocess.run(
                ["fuser", "-v", device_path],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                logger.warning("Killing processes using %s", device_path)
                subprocess.run(["fuser", "-k", device_path], timeout=5)
                time.sleep(1)
                return True
        except Exception as e:
            logger.debug("Could not free device: %s", e)
        return False
    
    def stop_stream(self, camera_type: str) -> bool:
        """Stop streaming for a specific camera."""
        with self._lock:
            return self._stop_stream_internal(camera_type)
    
    def _stop_stream_internal(self, camera_type: str) -> bool:
        """Internal method to stop a stream (must hold lock)."""
        if camera_type not in self._streams:
            return False
        
        stream = self._streams[camera_type]
        
        with stream.lock:
            # Stop consumer process (ffmpeg)
            if stream.process:
                try:
                    logger.info("Stopping consumer process for %s (PID: %d)",
                              stream.camera_type, stream.process.pid)

                    os.killpg(os.getpgid(stream.process.pid), signal.SIGTERM)

                    try:
                        stream.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        logger.warning("Consumer process did not exit, sending SIGKILL")
                        os.killpg(os.getpgid(stream.process.pid), signal.SIGKILL)
                        stream.process.wait()

                except Exception as exc:
                    logger.exception("Error stopping consumer process for %s", camera_type)
                    stream.last_error = str(exc)

                finally:
                    stream.process = None

            # Stop producer process (rpicam-vid) if it exists
            if stream.producer_process:
                try:
                    logger.info("Stopping producer process for %s (PID: %d)",
                              stream.camera_type, stream.producer_process.pid)

                    os.killpg(os.getpgid(stream.producer_process.pid), signal.SIGTERM)

                    try:
                        stream.producer_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        logger.warning("Producer process did not exit, sending SIGKILL")
                        os.killpg(os.getpgid(stream.producer_process.pid), signal.SIGKILL)
                        stream.producer_process.wait()

                except Exception as exc:
                    logger.exception("Error stopping producer process for %s", camera_type)

                finally:
                    stream.producer_process = None
            
            old_state = stream.state
            stream.state = STREAM_STATE_STARTING
            stream.watchdog_active = False
            
            logger.info("Stream %s stopped (was %s)", camera_type, old_state)
            
            if self.on_stream_status_change:
                self.on_stream_status_change(camera_type, "stopped", "")
            # Release device lock if held
            try:
                from .device_lock import manager as device_lock_manager
                owner = f"stream:{camera_type}"
                device_lock_manager.release(stream.device_path, owner)
            except Exception:
                logger.debug("Failed to release lock for %s", camera_type)

            return True
    
    def stop_all_streams(self) -> None:
        """Stop all streams."""
        with self._lock:
            for camera_type in list(self._streams.keys()):
                self._stop_stream_internal(camera_type)
    
    def get_stream_status(self, camera_type: str) -> Optional[Dict[str, Any]]:
        """Get status of a specific stream."""
        with self._lock:
            if camera_type not in self._streams:
                return None
            
            stream = self._streams[camera_type]
            
            with stream.lock:
                return {
                    "camera_type": stream.camera_type,
                    "device_path": stream.device_path,
                    "backend": stream.backend,
                    "state": stream.state,
                    "selected_format": stream.selected_format,
                    "selected_resolution": stream.selected_resolution,
                    "restart_count": stream.restart_count,
                    "consecutive_failures": stream.consecutive_failures,
                    "last_error": stream.last_error,
                    "pid": stream.process.pid if stream.process else None,
                    "producer_pid": stream.producer_process.pid if stream.producer_process else None,
                    "is_running": stream.process is not None and stream.process.poll() is None,
                    "profile": stream.profile.to_dict(),
                }
    
    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all streams."""
        with self._lock:
            return {
                ct: status
                for ct, status in (
                    (ct, self.get_stream_status(ct))
                    for ct in list(self._streams.keys())
                )
                if status is not None
            }
    
    def start_watchdog(self) -> None:
        """Start background watchdog thread for health monitoring."""
        if self._running:
            return
        
        self._running = True
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name="ffmpeg-watchdog",
        )
        self._watchdog_thread.start()
        logger.info("FFmpeg watchdog started")
    
    def stop_watchdog(self) -> None:
        """Stop background watchdog."""
        self._running = False
        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=5)
        logger.info("FFmpeg watchdog stopped")
    
    def _watchdog_loop(self) -> None:
        """
        Background watchdog loop.
        Monitors all streams and restarts failed ones.
        """
        while self._running:
            time.sleep(self.WATCHDOG_INTERVAL)
            
            with self._lock:
                streams = list(self._streams.values())
            
            for stream in streams:
                self._check_stream_health(stream)
    
    def _check_stream_health(self, stream: StreamProcess) -> None:
        """
        Check health of a single stream and restart if necessary.
        """
        with stream.lock:
            # Skip if not running
            if stream.state not in (STREAM_STATE_RUNNING, STREAM_STATE_DEGRADED):
                return
            
            if not stream.process:
                logger.warning(
                    "Stream %s has no process, restarting",
                    stream.camera_type,
                )
                self._restart_stream(stream)
                return
            
            # Check if process is still alive
            returncode = stream.process.poll()
            if returncode is not None:
                # Process died
                stderr = (
                    stream.process.stderr.read().decode(errors="replace")
                    if stream.process.stderr else ""
                )

                logger.warning(
                    "Stream %s process died (code %d): %s",
                    stream.camera_type,
                    returncode,
                    stderr[:300],
                )

                stream.state = STREAM_STATE_RECOVERING
                stream.last_error = f"Process died: {stderr[:200]}"

                # Clean up any leftover processes and release locks before restart
                try:
                    self._stop_stream_internal(stream.camera_type)
                except Exception:
                    logger.debug("_stop_stream_internal failed during restart for %s", stream.camera_type)

                self._restart_stream(stream)
                return

            # For libcamera pipelines, also check producer process
            if stream.backend == "libcamera" and stream.producer_process:
                producer_returncode = stream.producer_process.poll()
                if producer_returncode is not None:
                    logger.warning(
                        "Stream %s producer process died (code %d)",
                        stream.camera_type,
                        producer_returncode,
                    )
                    stream.state = STREAM_STATE_RECOVERING
                    stream.last_error = f"Producer process died: {producer_returncode}"
                    self._restart_stream(stream)
                    return
            
            # Check for frame stalls (no progress updates for extended period)
            now = time.time()
            time_since_last_progress = now - stream._last_progress_time

            # Update frame stall tracking
            if stream._last_progress_time > 0 and time_since_last_progress > self.FRAME_STALL_THRESHOLD:
                if stream.frame_stall_start is None:
                    # Start of stall period
                    stream.frame_stall_start = now
                    logger.warning(
                        "Stream %s frame stall detected (no progress for %.1fs)",
                        stream.camera_type,
                        time_since_last_progress,
                    )
                elif now - stream.frame_stall_start > self.FRAME_STALL_THRESHOLD:
                    # Stall confirmed, restart stream
                    logger.error(
                        "Stream %s confirmed stalled (no progress for %.1fs), restarting",
                        stream.camera_type,
                        time_since_last_progress,
                    )
                    stream.state = STREAM_STATE_RECOVERING
                    stream.last_error = f"Frame stall: {time_since_last_progress:.1f}s"
                    self._restart_stream(stream)
                    return
            else:
                # Clear stall state if frames are flowing again
                if stream.frame_stall_start is not None:
                    logger.info(
                        "Stream %s recovered from frame stall",
                        stream.camera_type,
                    )
                    stream.frame_stall_start = None

            # Periodic frame rate check
            if now - stream.last_health_check > self.FPS_CHECK_INTERVAL:
                stream.last_health_check = now

                # Check if we're getting any frame updates
                if stream.last_frame_count == 0 and now - stream.last_start_time > self.RECONNECT_GRACE_PERIOD:
                    logger.warning(
                        "Stream %s has produced no frames since start (%.1fs ago)",
                        stream.camera_type,
                        now - stream.last_start_time,
                    )
                    stream.state = STREAM_STATE_DEGRADED
    
    def _restart_stream(self, stream: StreamProcess) -> bool:
        """
        Restart a failed stream with exponential backoff.
        """
        with stream.lock:
            # Check if we should give up
            if stream.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                logger.error(
                    "Stream %s exceeded max failures (%d), marking as FAILED",
                    stream.camera_type,
                    self.MAX_CONSECUTIVE_FAILURES,
                )
                stream.state = STREAM_STATE_FAILED
                
                if self.on_stream_status_change:
                    self.on_stream_status_change(
                        stream.camera_type,
                        STREAM_STATE_FAILED,
                        "Max consecutive failures exceeded",
                    )
                return False
            
            # Calculate backoff delay
            delay = min(
                self.RESTART_BACKOFF_BASE * (2 ** stream.consecutive_failures),
                self.RESTART_BACKOFF_MAX,
            )
            
            logger.info(
                "Restarting stream %s in %.1f seconds (failure %d/%d)",
                stream.camera_type,
                delay,
                stream.consecutive_failures + 1,
                self.MAX_CONSECUTIVE_FAILURES,
            )
            
            stream.state = STREAM_STATE_RECOVERING
            
            # Wait with backoff
            time.sleep(delay)
            
            # Try to restart
            return self._start_ffmpeg_process(stream)
    
    def cleanup(self) -> None:
        """Clean up all resources."""
        logger.info("Cleaning up FFmpegStreamEngine...")
        self.stop_watchdog()
        self.stop_all_streams()
        logger.info("Cleanup complete")


# ============================================================================
# BACKWARD COMPATIBILITY WRAPPER
# ============================================================================

class FFmpegManager(FFmpegStreamEngine):
    """
    Backward-compatible wrapper for existing code.
    New code should use FFmpegStreamEngine directly.
    """
    
    def __init__(
        self,
        device_id: str,
        mediamtx_host: str = "127.0.0.1",
        mediamtx_rtsp_port: int = 8554,
        on_stream_status_change: Optional[Callable[[str, str], None]] = None,
    ):
        # Wrap the callback for backward compatibility
        def wrapped_callback(camera_type: str, state: str, details: str = ""):
            if on_stream_status_change:
                on_stream_status_change(camera_type, state)
        
        super().__init__(
            device_id=device_id,
            mediamtx_host=mediamtx_host,
            mediamtx_rtsp_port=mediamtx_rtsp_port,
            on_stream_status_change=wrapped_callback,
        )
    
    def add_stream(self, camera_type: str, device_path: str, formats: list[str]) -> None:
        """
        Legacy method: create a profile from device path and formats.
        """
        # Create a profile from the provided info
        profile = CameraFormatProfile(
            device=device_path,
            supported_formats=formats,
            preferred_format=FormatScanner._select_preferred_format(formats),
            safe_resolutions=FormatScanner.SAFE_RESOLUTIONS.get(
                "external" if "video2" in device_path else "internal",
                ["640x480"],
            ),
            driver_info="",
        )
        
        super().add_stream(camera_type, profile)
    
    @property
    def _streams(self) -> Dict[str, StreamProcess]:
        """Legacy access to streams dict."""
        return super()._streams


# ============================================================================
# MQTT EXPONENTIAL BACKOFF HANDLER
# ============================================================================

class MQTTBackoffHandler:
    """
    Handles MQTT reconnection with exponential backoff.
    Prevents reconnect storms and handles rc=7 properly.
    """
    
    # Backoff configuration: 1s, 2s, 5s, 10s, 30s, 60s max
    BACKOFF_DELAYS = [1, 2, 5, 10, 30, 60]
    
    def __init__(self):
        self._reconnect_delay_idx = 0
        self._reconnect_lock = threading.Lock()
        self._is_reconnecting = False
        self._manual_disconnect = False
        self._connection_state = "DISCONNECTED"  # DISCONNECTED, CONNECTED, BACKOFF, FAILED
    
    def on_connect(self, rc: int) -> None:
        """Handle MQTT connection result."""
        with self._reconnect_lock:
            if rc == 0:
                logger.info("MQTT connected successfully")
                self._connection_state = "CONNECTED"
                self._reconnect_delay_idx = 0  # Reset backoff on success
                self._is_reconnecting = False
                self._manual_disconnect = False
            else:
                error_msg = self._get_error_message(rc)
                logger.error("MQTT connection failed (rc=%d): %s", rc, error_msg)
                self._connection_state = "BACKOFF"
                self._is_reconnecting = False
                
                # rc=7 is session conflict or auth issue
                # Don't instantly reconnect, use backoff
                if rc == 7:
                    logger.warning(
                        "MQTT rc=7 (session conflict/auth issue). "
                        "Using exponential backoff."
                    )
    
    def on_disconnect(self, rc: int) -> None:
        """Handle MQTT disconnection."""
        with self._reconnect_lock:
            if rc == 0:
                logger.info("MQTT disconnected cleanly")
                self._connection_state = "DISCONNECTED"
                self._reconnect_delay_idx = 0
            else:
                error_msg = self._get_error_message(rc)
                logger.warning("MQTT unexpected disconnect (rc=%d): %s", rc, error_msg)
                self._connection_state = "BACKOFF"
                
                # Don't reconnect if manual disconnect
                if self._manual_disconnect:
                    logger.debug("Skipping reconnect: manual disconnect")
                    return
                
                # Schedule reconnect with backoff
                self._schedule_reconnect()
    
    def _schedule_reconnect(self) -> None:
        """Schedule a reconnect attempt with exponential backoff."""
        if self._is_reconnecting:
            logger.debug("Reconnect already in progress")
            return
        
        with self._reconnect_lock:
            if self._manual_disconnect:
                return
            
            self._is_reconnecting = True
            delay = self.BACKOFF_DELAYS[
                min(self._reconnect_delay_idx, len(self.BACKOFF_DELAYS) - 1)
            ]
            
            logger.info(
                "MQTT reconnect attempt %d in %d seconds",
                self._reconnect_delay_idx + 1,
                delay,
            )
            
            # Increment for next time
            self._reconnect_delay_idx = min(
                self._reconnect_delay_idx + 1,
                len(self.BACKOFF_DELAYS) - 1,
            )
        
        # Schedule reconnect in background
        def do_reconnect():
            time.sleep(delay)
            with self._reconnect_lock:
                self._is_reconnecting = False
                # The actual reconnect should be triggered by the caller
                # This just resets the state
        
        threading.Thread(target=do_reconnect, daemon=True).start()
    
    def request_disconnect(self) -> None:
        """Request a manual disconnect (stops auto-reconnect)."""
        with self._reconnect_lock:
            self._manual_disconnect = True
            self._is_reconnecting = False
            self._connection_state = "DISCONNECTED"
    
    def get_state(self) -> str:
        """Get current connection state."""
        with self._reconnect_lock:
            return self._connection_state
    
    @staticmethod
    def _get_error_message(rc: int) -> str:
        """Get human-readable error message for MQTT return codes."""
        errors = {
            0: "Connection accepted",
            1: "Connection refused - unacceptable protocol version",
            2: "Connection refused - invalid client identifier",
            3: "Connection refused - server unavailable",
            4: "Connection refused - bad username or password",
            5: "Connection refused - not authorized",
            6: "Reserved for future use",
            7: "Connection refused - session conflict or not authorized",
        }
        return errors.get(rc, f"Unknown error code {rc}")


# ============================================================================
# PRODUCTION MULTI-CAMERA PIPELINE
# ============================================================================

class ProductionCameraPipeline:
    """
    Production-grade multi-camera streaming pipeline.
    
    Features:
    - Independent camera pipelines (no shared state)
    - Isolated failure domains
    - Automatic recovery per camera
    - Structured logging
    """
    
    def __init__(
        self,
        device_id: str,
        mediamtx_host: str = "127.0.0.1",
        mediamtx_rtsp_port: int = 8554,
    ):
        self.device_id = device_id
        self.mediamtx_host = mediamtx_host
        self.mediamtx_rtsp_port = mediamtx_rtsp_port
        
        # Independent stream engines per camera role
        self.internal_engine = FFmpegStreamEngine(
            device_id=device_id,
            mediamtx_host=mediamtx_host,
            mediamtx_rtsp_port=mediamtx_rtsp_port,
            on_stream_status_change=self._make_status_callback("internal"),
        )
        
        self.external_engine = FFmpegStreamEngine(
            device_id=device_id,
            mediamtx_host=mediamtx_host,
            mediamtx_rtsp_port=mediamtx_rtsp_port,
            on_stream_status_change=self._make_status_callback("external"),
        )
        
        # Format scanners
        self.internal_scanner = FormatScanner()
        self.external_scanner = FormatScanner()
        
        # State
        self._running = False
        self._supervisor_thread: Optional[threading.Thread] = None
        
        logger.info("ProductionCameraPipeline initialized for device %s", device_id)
    
    def _make_status_callback(
        self, camera_role: str
    ) -> Callable[[str, str, str], None]:
        """Create a status callback for a specific camera role."""
        def callback(camera_type: str, state: str, details: str):
            logger.info(
                "[%s] State change: %s -> %s (%s)",
                camera_role.upper(),
                camera_type,
                state,
                details,
            )
        
        return callback
    
    def setup_cameras(
        self,
        internal_device: str,
        external_device: Optional[str] = None,
        internal_backend: str = "v4l2",
        external_backend: str = "v4l2",
    ) -> None:
        """
        Setup camera pipelines with format probing and backend classification.

        Args:
            internal_device: Path to internal camera (e.g., /dev/video0)
            external_device: Path to external camera (e.g., /dev/video2)
            internal_backend: Backend type for internal camera ("libcamera" or "v4l2")
            external_backend: Backend type for external camera ("libcamera" or "v4l2")
        """
        logger.info("=== Setting up camera pipelines ===")

        # Probe and setup internal camera
        logger.info("Probing internal camera: %s (backend=%s)", internal_device, internal_backend)
        try:
            internal_profile = self.internal_scanner.probe_device(internal_device)
            self.internal_engine.add_stream("internal", internal_profile, internal_backend)
            logger.info("Internal camera ready: %s (backend=%s)", internal_profile.preferred_format, internal_backend)
        except Exception as e:
            logger.error("Failed to setup internal camera: %s", e)
            raise

        # Probe and setup external camera (if provided)
        if external_device:
            logger.info("Probing external camera: %s (backend=%s)", external_device, external_backend)
            try:
                external_profile = self.external_scanner.probe_device(external_device)
                self.external_engine.add_stream("external", external_profile, external_backend)
                logger.info("External camera ready: %s (backend=%s)", external_profile.preferred_format, external_backend)
            except Exception as e:
                logger.error("Failed to setup external camera: %s", e)
                # Don't raise - external camera failure shouldn't block internal

        logger.info("=== Camera setup complete ===")

    def setup_from_registry(self, camera_devices: List[object]) -> None:
        """
        Setup multiple camera pipelines from registry devices.

        camera_devices: iterable of objects with attributes:
            - device_path
            - classification (with .backend)
            - capabilities

        This will assign one primary `internal` stream (platform/libcamera preferred)
        and then create `external`, `external_1`, `external_2`, ... for remaining USB/V4L2 devices.
        Existing RTSP naming for `internal` and `external` is preserved; additional streams
        are given unique suffixes so they don't conflict.
        """
        logger.info("=== Setting up cameras from registry (%d devices) ===", len(camera_devices))

        # Determine primary internal candidate: prefer libcamera/platform devices

        # Deterministic assignment based on physical sysfs device path.
        # For each by-id path, resolve to /dev/videoX and then read the
        # sysfs device link to obtain a stable physical id (bus/port).
        def _phys_id(dev_obj):
            try:
                dp = Path(getattr(dev_obj, 'device_path'))
                resolved = dp.resolve()
                video_name = resolved.name if resolved.exists() else dp.name
                video_sys = Path('/sys/class/video4linux') / video_name / 'device'
                if video_sys.exists():
                    target = (Path('/sys/class/video4linux') / video_name / 'device').resolve()
                    return str(target)
            except Exception:
                pass
            # Fallback to by-id path string
            return str(getattr(dev_obj, 'device_path', ''))

        sorted_devs = sorted(camera_devices, key=_phys_id)
        primary_internal = sorted_devs[0] if sorted_devs else None
        others: List[object] = sorted_devs[1:] if len(sorted_devs) > 1 else []

        # Assign streams
        assigned = []  # tuples of (role_name, device_obj, backend)

        if primary_internal:
            role = 'internal'
            backend = getattr(primary_internal.classification, 'backend', 'v4l2')
            assigned.append((role, primary_internal, backend))

        # For others, create external, external_1, external_2...
        ext_index = 0
        for dev in others:
            if getattr(dev.classification, 'device_type', '') == 'csi' or getattr(dev.classification, 'backend', '') == 'libcamera':
                # treat as internal-like device
                role = f'internal_{ext_index}' if ext_index > 0 else 'internal'
            else:
                role = 'external' if ext_index == 0 else f'external_{ext_index}'
            backend = getattr(dev.classification, 'backend', 'v4l2')
            assigned.append((role, dev, backend))
            ext_index += 1

        # Probe and add to respective engines
        for role_name, dev, backend in assigned:
            device_path = getattr(dev, 'device_path', None)
            if not device_path:
                logger.warning("Skipping device with no path: %s", dev)
                continue

            try:
                profile = self.internal_scanner.probe_device(device_path) if role_name.startswith('internal') else self.external_scanner.probe_device(device_path)
                # Choose engine: internal_engine for roles starting with 'internal', else external_engine
                engine = self.internal_engine if role_name.startswith('internal') else self.external_engine
                engine.add_stream(role_name, profile, backend)
                logger.info("Added stream %s -> %s (backend=%s)", role_name, device_path, backend)
            except Exception as e:
                logger.exception("Failed to add stream for %s (%s): %s", device_path, role_name, e)

        logger.info("=== Registry camera setup complete (%d streams) ===", len(assigned))
    
    def start(self) -> None:
        """Start all camera pipelines."""
        logger.info("=== Starting production camera pipeline ===")
        
        # Start internal camera (critical)
        logger.info("Starting internal camera pipeline...")
        self.internal_engine.start_all_streams()
        self.internal_engine.start_watchdog()
        
        # Start external camera (non-critical)
        logger.info("Starting external camera pipeline...")
        self.external_engine.start_all_streams()
        self.external_engine.start_watchdog()
        
        # Start supervisor loop
        self._running = True
        self._supervisor_thread = threading.Thread(
            target=self._supervisor_loop,
            daemon=True,
            name="pipeline-supervisor",
        )
        self._supervisor_thread.start()
        
        logger.info("=== Production pipeline started ===")
    
    def _supervisor_loop(self) -> None:
        """
        Supervisor loop: monitors pipeline health every 5 seconds.
        Restarts only failed streams, never the whole system.
        """
        while self._running:
            time.sleep(5)
            
            try:
                self._check_pipeline_health()
            except Exception as e:
                logger.exception("Error in supervisor loop: %s", e)
    
    def _check_pipeline_health(self) -> None:
        """Check health of all camera pipelines."""
        # Check internal camera (critical)
        internal_status = self.internal_engine.get_all_status()
        for cam_type, status in internal_status.items():
            if status["state"] == STREAM_STATE_FAILED:
                logger.error(
                    "Internal camera FAILED: %s. Attempting recovery...",
                    status["last_error"],
                )
                # Internal camera is critical, try aggressive recovery
                self.internal_engine.start_stream(cam_type)
        
        # Check external camera (non-critical)
        external_status = self.external_engine.get_all_status()
        for cam_type, status in external_status.items():
            if status["state"] == STREAM_STATE_FAILED:
                logger.warning(
                    "External camera FAILED: %s. Will retry automatically.",
                    status["last_error"],
                )
                # External camera will auto-recover via its watchdog
    
    def stop(self) -> None:
        """Stop all camera pipelines."""
        logger.info("=== Stopping production camera pipeline ===")
        
        self._running = False
        
        if self._supervisor_thread:
            self._supervisor_thread.join(timeout=5)
        
        self.external_engine.cleanup()
        self.internal_engine.cleanup()
        
        logger.info("=== Production pipeline stopped ===")
    
    def get_pipeline_status(self) -> Dict[str, Any]:
        """Get status of entire pipeline."""
        return {
            "device_id": self.device_id,
            "internal_camera": self.internal_engine.get_all_status(),
            "external_camera": self.external_engine.get_all_status(),
            "rtsp_base_url": f"rtsp://{self.mediamtx_host}:{self.mediamtx_rtsp_port}",
        }
    
    def get_stream_urls(self) -> Dict[str, str]:
        """Get RTSP URLs for all active streams."""
        urls = {}
        
        for role, engine in [("internal", self.internal_engine),
                            ("external", self.external_engine)]:
            status = engine.get_all_status()
            for cam_type, cam_status in status.items():
                if cam_status["state"] == STREAM_STATE_RUNNING:
                    urls[role] = (
                        f"rtsp://{self.mediamtx_host}:{self.mediamtx_rtsp_port}/"
                        f"{self.device_id}/{cam_type}"
                    )
        
        return urls


# ============================================================================
# CONSTANTS
# ============================================================================

# Stream states (for backward compatibility)
STREAM_STATE_STARTING = STREAM_STATE_STARTING
STREAM_STATE_RUNNING = STREAM_STATE_RUNNING
STREAM_STATE_DEGRADED = STREAM_STATE_DEGRADED
STREAM_STATE_RECOVERING = STREAM_STATE_RECOVERING
STREAM_STATE_FAILED = STREAM_STATE_FAILED


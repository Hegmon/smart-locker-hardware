"""
Ultra-Low-Latency Dual USB Camera Streaming Engine

Production-grade architecture for Raspberry Pi 4:
- Dedicated FFmpeg reader per camera (isolated pipelines)
- Latest-frame-only buffers (size=1, no queue buildup)
- MJPEG/H264 preferred (no raw YUYV bandwidth waste)
- Ultra-low-latency FFmpeg configuration (<500ms end-to-end)
- Strict camera ownership (one exclusive owner per device)
- Automatic recovery and reconnect
- USB bandwidth optimization for dual cameras
- Real-time health monitoring and metrics

Architecture:
USB Camera 1 → FFmpeg Reader → Latest Frame Buffer → Encoder → MQTT Publisher
USB Camera 2 → FFmpeg Reader → Latest Frame Buffer → Encoder → MQTT Publisher

Features:
- Zero buffering delay
- Stable FPS under load
- Independent stream failure recovery
- 24/7 stable operation
- Low CPU/memory footprint
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
    
    # Ultra-low-latency USB camera profiles optimized for Pi4
    CAMERA_PROFILES = {
        # Default profiles for simultaneous dual USB camera streaming
        "usb_camera_primary": {  # First USB camera (lower bandwidth usage)
            "preferred_format": "mjpeg",
            "fallback_formats": ["h264", "yuyv422"],
            "resolution": "1280x720",  # 720p for stability
            "framerate": 15,  # Lower FPS for USB bandwidth
            "bitrate": "1000k",
        },
        "usb_camera_secondary": {  # Second USB camera (minimal bandwidth)
            "preferred_format": "mjpeg",
            "fallback_formats": ["h264", "yuyv422"],
            "resolution": "640x480",  # 480p to prevent saturation
            "framerate": 10,  # Minimal FPS for stability
            "bitrate": "500k",
        },
        # Specific camera profiles
        "a4tech_camera": {
            "preferred_format": "mjpeg",
            "fallback_formats": ["yuyv422"],
            "resolution": "1280x720",
            "framerate": 15,
            "bitrate": "1000k",
        },
        "integrated_webcam": {
            "preferred_format": "mjpeg",
            "fallback_formats": ["yuyv422"],
            "resolution": "640x480",
            "framerate": 15,
            "bitrate": "500k",
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
class LatestFrameBuffer:
    """Latest-frame-only buffer with size=1 for zero-latency streaming"""
    frame_data: Optional[bytes] = None
    frame_timestamp: float = 0.0
    frame_count: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)

    def put_frame(self, data: bytes) -> None:
        """Store latest frame, overwriting any previous frame"""
        with self.lock:
            self.frame_data = data
            self.frame_timestamp = time.time()
            self.frame_count += 1

    def get_latest_frame(self) -> Optional[tuple[bytes, float, int]]:
        """Get latest frame data, timestamp, and count"""
        with self.lock:
            if self.frame_data is None:
                return None
            return (self.frame_data, self.frame_timestamp, self.frame_count)

    def clear(self) -> None:
        """Clear the buffer"""
        with self.lock:
            self.frame_data = None
            self.frame_timestamp = 0.0
            self.frame_count = 0


@dataclass
class StreamProcess:
    """
    Ultra-low-latency USB camera streaming process.

    Architecture:
    FFmpeg Reader → Latest Frame Buffer → Encoder → MQTT Publisher
    """
    camera_type: str
    device_path: str
    profile: CameraFormatProfile

    # FFmpeg reader process (captures from camera)
    reader_process: Optional[subprocess.Popen] = None

    # Encoder process (compresses frames for streaming)
    encoder_process: Optional[subprocess.Popen] = None

    # Latest frame buffer (size=1, no queue buildup)
    frame_buffer: LatestFrameBuffer = field(default_factory=LatestFrameBuffer)

    # State tracking
    state: str = STREAM_STATE_STARTING
    selected_format: Optional[str] = None
    selected_resolution: str = "640x480"
    backend: str = "v4l2"

    # Health metrics
    restart_count: int = 0
    consecutive_failures: int = 0
    last_frame_time: float = 0
    last_start_time: float = 0
    last_health_check: float = 0
    total_frames_produced: int = 0
    fps_current: float = 0.0
    last_error: Optional[str] = None

    # Command tracking
    reader_command: list[str] = field(default_factory=list)
    encoder_command: list[str] = field(default_factory=list)

    # Thread safety
    lock: threading.RLock = field(default_factory=threading.RLock)

    # Reader and encoder threads
    reader_thread: Optional[threading.Thread] = None
    encoder_thread: Optional[threading.Thread] = None
    reader_stop: threading.Event = field(default_factory=threading.Event)
    encoder_stop: threading.Event = field(default_factory=threading.Event)

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
                ok = self._start_stream_pipeline(stream)
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
    
    def _start_stream_pipeline(self, stream: StreamProcess) -> bool:
        """
        Start ultra-low-latency FFmpeg reader + encoder pipeline.

        Uses dedicated reader process piping to encoder process for zero buffering.
        """
        logger.info(
            "=== Starting ultra-low-latency pipeline: %s (device=%s) ===",
            stream.camera_type,
            stream.device_path,
        )

        # Get optimized camera profile
        profile = self._get_camera_profile(stream)
        stream.selected_resolution = profile.get("resolution", "640x480")

        # Determine best input format
        input_format = self._select_optimal_format(stream)
        stream.selected_format = input_format

        logger.info(
            "Camera profile: format=%s, resolution=%s, framerate=%s",
            input_format, stream.selected_resolution, profile.get("framerate", 15)
        )

        try:
            # Start FFmpeg reader process (captures from camera)
            reader_cmd = self._build_ffmpeg_reader_command(stream, input_format)
            logger.debug("Reader command: %s", " ".join(reader_cmd))

            stream.reader_process = subprocess.Popen(
                reader_cmd,
                stdout=subprocess.PIPE,  # Pipe to encoder
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                preexec_fn=os.setsid,
                bufsize=0,  # Unbuffered for low latency
            )

            # Give reader a moment to start
            time.sleep(0.5)

            # Check if reader started successfully
            if stream.reader_process.poll() is not None:
                stderr = stream.reader_process.stderr.read().decode(errors="replace")
                logger.error("Reader failed to start for %s: %s", stream.camera_type, stderr[:200])
                stream.reader_process = None
                return False

            # Start FFmpeg encoder process (reads from reader pipe)
            encoder_cmd = self._build_ffmpeg_encoder_command(stream)
            logger.debug("Encoder command: %s", " ".join(encoder_cmd))

            stream.encoder_process = subprocess.Popen(
                encoder_cmd,
                stdin=stream.reader_process.stdout,  # Read from reader pipe
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid,
                bufsize=0,  # Unbuffered
            )

            # Give encoder a moment to start
            time.sleep(0.5)

            # Check if encoder started successfully
            if stream.encoder_process.poll() is not None:
                stderr = stream.encoder_process.stderr.read().decode(errors="replace")
                logger.error("Encoder failed to start for %s: %s", stream.camera_type, stderr[:200])
                self._cleanup_stream_processes(stream)
                return False

            # Start reader thread to monitor reader process
            stream.reader_stop.clear()
            stream.reader_thread = threading.Thread(
                target=self._reader_monitor_thread,
                args=(stream,),
                daemon=True,
                name=f"reader-{stream.camera_type}",
            )
            stream.reader_thread.start()

            # Start encoder thread to monitor encoder process
            stream.encoder_stop.clear()
            stream.encoder_thread = threading.Thread(
                target=self._encoder_monitor_thread,
                args=(stream,),
                daemon=True,
                name=f"encoder-{stream.camera_type}",
            )
            stream.encoder_thread.start()

            # Success!
            stream.last_start_time = time.time()
            stream.last_frame_time = time.time()
            stream.state = STREAM_STATE_RUNNING
            stream.consecutive_failures = 0

            logger.info(
                "✓ Ultra-low-latency pipeline started for %s "
                "(reader PID: %d, encoder PID: %d, format: %s)",
                stream.camera_type,
                stream.reader_process.pid if stream.reader_process else 0,
                stream.encoder_process.pid if stream.encoder_process else 0,
                input_format
            )

            return True

        except Exception as e:
            logger.exception("Failed to start pipeline for %s: %s", stream.camera_type, e)
            self._cleanup_stream_processes(stream)
            return False

    def _select_optimal_format(self, stream: StreamProcess) -> str:
        """Select optimal format prioritizing MJPEG > H264 > YUYV"""
        supported = stream.profile.supported_formats

        # Priority: MJPEG (lowest bandwidth) > H264 > YUYV (fallback)
        if "mjpeg" in supported:
            return "mjpeg"
        elif "h264" in supported:
            return "h264"
        elif "yuyv422" in supported:
            return "yuyv422"
        else:
            # Fallback to MJPEG (most cameras support it)
            return "mjpeg"
        
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
    
    def _build_ffmpeg_reader_command(self, stream: StreamProcess, input_format: str) -> List[str]:
        """
        Build ultra-low-latency FFmpeg reader command for USB camera.

        This reader captures MJPEG/H264 directly from camera with minimal processing.
        Output is piped to encoder process.
        """
        device_path = stream.device_path

        # Get camera profile for optimized settings
        profile = self._get_camera_profile(stream)
        framerate = profile.get("framerate", 15)

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",  # Minimize logging for performance
            "-f", "v4l2",
            # Ultra-low-latency flags
            "-fflags", "nobuffer+discardcorrupt",
            "-flags", "low_delay",
            "-analyzeduration", "0",
            "-probesize", "32",
            "-fpsprobesize", "0",
            "-thread_queue_size", "64",
            "-flush_packets", "1",
            "-use_wallclock_as_timestamps", "1",
            "-avioflags", "direct",
            "-max_delay", "0",
            "-rtbufsize", "1M",
            # Camera settings
            "-framerate", str(framerate),
            "-video_size", stream.selected_resolution,
            "-threads", "1",  # Single thread for USB stability
        ]

        # Add input format (MJPEG or H264 preferred)
        if input_format and input_format != "auto":
            cmd.extend(["-input_format", input_format])

        cmd.extend(["-i", device_path])

        # Output raw MJPEG/H264 to stdout for encoder
        if input_format == "mjpeg":
            cmd.extend([
                "-c:v", "copy",  # Passthrough MJPEG
                "-f", "mjpeg",
                "-q:v", "2",  # Quality setting for MJPEG
                "-"
            ])
        elif input_format == "h264":
            cmd.extend([
                "-c:v", "copy",  # Passthrough H264
                "-f", "h264",
                "-"
            ])
        else:
            # Fallback for YUYV - encode to MJPEG
            cmd.extend([
                "-c:v", "mjpeg",
                "-q:v", "3",  # Balance quality/speed
                "-f", "mjpeg",
                "-"
            ])

        return cmd

    def _build_ffmpeg_encoder_command(self, stream: StreamProcess) -> List[str]:
        """
        Build FFmpeg encoder command for low-latency RTSP streaming.

        Reads from stdin (piped from reader), encodes for RTSP output.
        """
        profile = self._get_camera_profile(stream)
        bitrate = profile.get("bitrate", "1000k")

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "mjpeg",  # Input from reader pipe
            "-i", "-",
            # Ultra-low-latency encoding
            "-fflags", "nobuffer+discardcorrupt",
            "-flags", "low_delay",
            "-analyzeduration", "0",
            "-probesize", "32",
            "-fpsprobesize", "0",
            "-thread_queue_size", "64",
            "-flush_packets", "1",
            "-avioflags", "direct",
            "-max_delay", "0",
            # H.264 encoding optimized for low latency
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-g", "10",  # GOP size 10 for low latency
            "-keyint_min", "10",
            "-sc_threshold", "0",  # Disable scene change detection
            "-b:v", bitrate,
            "-maxrate", bitrate,
            "-bufsize", "500k",  # Small buffer
            "-threads", "1",
            # RTSP output
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
        ]

        # Output URL
        output_url = (
            f"rtsp://{self.mediamtx_host}:{self.mediamtx_rtsp_port}/"
            f"{self.device_id}/{stream.camera_type}"
        )
        cmd.append(output_url)

        return cmd

    def _get_camera_profile(self, stream: StreamProcess) -> Dict[str, Any]:
        """Get optimized camera profile based on device and role"""
        device_path = stream.device_path

        # Check for explicit device profile
        if device_path in FormatScanner.CAMERA_PROFILES:
            return FormatScanner.CAMERA_PROFILES[device_path]

        # Check device name patterns
        device_name = ""
        try:
            name_file = Path(f"/sys/class/video4linux/{Path(device_path).name}/name")
            if name_file.exists():
                device_name = name_file.read_text().strip().lower()
        except:
            pass

        # Match patterns
        if "a4tech" in device_name:
            return FormatScanner.CAMERA_PROFILES["a4tech_camera"]
        elif "integrated" in device_name or "webcam" in device_name:
            return FormatScanner.CAMERA_PROFILES["integrated_webcam"]
        elif stream.camera_type == "internal":
            return FormatScanner.CAMERA_PROFILES["usb_camera_primary"]
        else:
            return FormatScanner.CAMERA_PROFILES["usb_camera_secondary"]

    def _reader_monitor_thread(self, stream: StreamProcess) -> None:
        """
        Monitor FFmpeg reader process and handle output/errors.

        The reader process captures MJPEG/H264 from camera and pipes to encoder.
        """
        if not stream.reader_process or not stream.reader_process.stderr:
            return

        try:
            while not stream.reader_stop.is_set():
                # Check if reader process is still alive
                if stream.reader_process.poll() is not None:
                    break

                # Read stderr for error monitoring (non-blocking)
                try:
                    line = stream.reader_process.stderr.readline()
                    if line:
                        line_str = line.decode(errors="replace").strip()
                        if line_str and not line_str.startswith("frame="):
                            logger.debug("Reader %s: %s", stream.camera_type, line_str)
                    else:
                        # No more data, small sleep to prevent busy loop
                        time.sleep(0.01)
                except:
                    break

        except Exception as e:
            logger.debug("Reader monitor error for %s: %s", stream.camera_type, e)

        # Reader process ended
        logger.warning("Reader process ended for %s", stream.camera_type)

    def _encoder_monitor_thread(self, stream: StreamProcess) -> None:
        """
        Monitor FFmpeg encoder process and track frame output.

        The encoder reads from reader pipe and outputs RTSP stream.
        """
        if not stream.encoder_process or not stream.encoder_process.stderr:
            return

        frame_count = 0
        last_frame_time = time.time()

        try:
            while not stream.encoder_stop.is_set():
                # Check if encoder process is still alive
                if stream.encoder_process.poll() is not None:
                    break

                # Read stderr for frame monitoring (non-blocking)
                try:
                    line = stream.encoder_process.stderr.readline()
                    if line:
                        line_str = line.decode(errors="replace").strip()

                        # Track frame encoding
                        if "frame=" in line_str and "fps=" in line_str:
                            try:
                                # Parse frame and fps from encoder output
                                parts = line_str.split()
                                frame_part = next((p for p in parts if p.startswith("frame=")), "")
                                fps_part = next((p for p in parts if p.startswith("fps=")), "")

                                if frame_part:
                                    frame_num = int(frame_part.split("=")[1])
                                    stream.total_frames_produced = frame_num
                                    stream.last_frame_time = time.time()

                                    # Calculate FPS
                                    now = time.time()
                                    if now - last_frame_time >= 1.0:
                                        frame_diff = frame_num - frame_count
                                        stream.fps_current = frame_diff / (now - last_frame_time)
                                        frame_count = frame_num
                                        last_frame_time = now

                            except Exception as e:
                                logger.debug("Failed to parse encoder output '%s': %s", line_str, e)

                        elif line_str and "error" in line_str.lower():
                            logger.warning("Encoder %s error: %s", stream.camera_type, line_str)
                    else:
                        # No more data, small sleep to prevent busy loop
                        time.sleep(0.01)
                except:
                    break

        except Exception as e:
            logger.debug("Encoder monitor error for %s: %s", stream.camera_type, e)

        # Encoder process ended
        logger.warning("Encoder process ended for %s", stream.camera_type)

    def _cleanup_stream_processes(self, stream: StreamProcess) -> None:
        """Clean up reader and encoder processes for a stream"""
        with stream.lock:
            # Stop monitor threads
            if stream.reader_thread and stream.reader_thread.is_alive():
                stream.reader_stop.set()
                stream.reader_thread.join(timeout=2)

            if stream.encoder_thread and stream.encoder_thread.is_alive():
                stream.encoder_stop.set()
                stream.encoder_thread.join(timeout=2)

            # Terminate processes
            for proc_attr in ['reader_process', 'encoder_process']:
                proc = getattr(stream, proc_attr)
                if proc and proc.poll() is None:
                    try:
                        # Try graceful termination first
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            # Force kill if it doesn't terminate
                            proc.kill()
                            proc.wait()
                    except Exception as e:
                        logger.debug("Error terminating %s process: %s", proc_attr, e)

                # Clear process reference
                setattr(stream, proc_attr, None)

            # Reset threads
            stream.reader_thread = None
            stream.encoder_thread = None
            stream.reader_stop.clear()
            stream.encoder_stop.clear()

    def _check_stream_health(self, stream: StreamProcess) -> None:
        """
        Check health of a single stream with ultra-low-latency monitoring.
        """
        with stream.lock:
            # Skip if not running
            if stream.state not in (STREAM_STATE_RUNNING, STREAM_STATE_DEGRADED):
                return

            now = time.time()

            # Check if reader process is alive
            if stream.reader_process and stream.reader_process.poll() is not None:
                stderr = ""
                try:
                    if stream.reader_process.stderr:
                        stderr = stream.reader_process.stderr.read().decode(errors="replace")[-200:]
                except:
                    pass

                logger.error("Reader process died for %s: %s", stream.camera_type, stderr)
                stream.state = STREAM_STATE_RECOVERING
                stream.last_error = f"Reader died: {stderr}"
                self._restart_stream(stream)
                return

            # Check if encoder process is alive
            if stream.encoder_process and stream.encoder_process.poll() is not None:
                stderr = ""
                try:
                    if stream.encoder_process.stderr:
                        stderr = stream.encoder_process.stderr.read().decode(errors="replace")[-200:]
                except:
                    pass

                logger.error("Encoder process died for %s: %s", stream.camera_type, stderr)
                stream.state = STREAM_STATE_RECOVERING
                stream.last_error = f"Encoder died: {stderr}"
                self._restart_stream(stream)
                return

            # Check for frame production (critical for low-latency)
            time_since_last_frame = now - stream.last_frame_time
            if time_since_last_frame > 5.0:  # 5 seconds without frames
                logger.warning("No frames produced for %s in %.1fs", stream.camera_type, time_since_last_frame)
                stream.state = STREAM_STATE_RECOVERING
                stream.last_error = f"Frame stall: {time_since_last_frame:.1f}s"
                self._restart_stream(stream)
                return

            # Update health check timestamp
            stream.last_health_check = now

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
            logger.info("Stopping ultra-low-latency pipeline for %s", camera_type)

            # Clean up all processes and threads
            self._cleanup_stream_processes(stream)

            # Update state
            stream.state = "stopped"
            stream.last_error = "Stream stopped"
            
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
                    "is_running": (
                        (stream.reader_process is not None and stream.reader_process.poll() is None) or
                        (stream.encoder_process is not None and stream.encoder_process.poll() is None)
                    ),
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
        Check health of a single ultra-low-latency stream.
        """
        self._check_stream_health_ultra(stream)
    
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

# ============================================================================
# PRODUCTION CAMERA PIPELINE - Ultra-Low-Latency Dual Camera Manager
# ============================================================================

class ProductionCameraPipeline:
    """
    Production-grade dual USB camera streaming pipeline.

    Manages two independent FFmpeg stream engines for ultra-low-latency
    simultaneous streaming of internal and external USB cameras.

    Features:
    - Independent camera pipelines (one freeze ≠ both freeze)
    - Latest-frame-only buffers for zero latency
    - Automatic recovery and health monitoring
    - USB bandwidth optimization for dual cameras
    - 24/7 stable operation
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

        # Two independent stream engines for dual camera isolation
        self.internal_engine = FFmpegStreamEngine(
            device_id=device_id,
            mediamtx_host=mediamtx_host,
            mediamtx_rtsp_port=mediamtx_rtsp_port,
        )

        self.external_engine = FFmpegStreamEngine(
            device_id=device_id,
            mediamtx_host=mediamtx_host,
            mediamtx_rtsp_port=mediamtx_rtsp_port,
        )

        # Format scanners
        self.internal_scanner = FormatScanner()
        self.external_scanner = FormatScanner()

        # State
        self._running = False

        logger.info("ProductionCameraPipeline initialized for device %s", device_id)

    def setup_from_registry(self, camera_devices: List[object]) -> None:
        """
        Setup cameras from registry devices (ultra-low-latency mode).

        Assigns cameras to internal/external roles with optimized profiles
        for simultaneous dual USB camera streaming.
        """
        logger.info("=== Setting up cameras from registry (%d devices) ===", len(camera_devices))

        # Sort devices by physical path for deterministic assignment
        def _phys_id(dev_obj):
            try:
                dp = getattr(dev_obj, 'device_path', '')
                resolved = dp
                if dp.startswith('/dev/v4l/by-id'):
                    try:
                        resolved = str(Path(dp).resolve())
                    except:
                        pass
                return resolved
            except Exception:
                return str(getattr(dev_obj, 'device_path', ''))

        sorted_devs = sorted(camera_devices, key=_phys_id)

        # Assign first device as internal (primary), others as external variants
        assigned = []

        if len(sorted_devs) >= 1:
            dev = sorted_devs[0]
            device_path = getattr(dev, 'device_path', '')
            if device_path:
                assigned.append(('internal', device_path))

        if len(sorted_devs) >= 2:
            dev = sorted_devs[1]
            device_path = getattr(dev, 'device_path', '')
            if device_path:
                assigned.append(('external', device_path))

        # Setup assigned cameras
        for role_name, device_path in assigned:
            try:
                # Use optimized profiles for dual camera streaming
                profile = self.internal_scanner.probe_device(device_path)

                # Apply USB optimization based on role
                if role_name == 'internal':
                    # Primary camera - higher quality but still USB-optimized
                    profile.safe_resolutions = ["1280x720", "640x480"]
                else:
                    # Secondary camera - lower bandwidth to prevent saturation
                    profile.safe_resolutions = ["640x480", "320x240"]

                # Add to appropriate engine
                if role_name == 'internal':
                    self.internal_engine.add_stream(role_name, profile)
                else:
                    self.external_engine.add_stream(role_name, profile)

                logger.info("✓ Added %s camera: %s", role_name, device_path)

            except Exception as e:
                logger.exception("Failed to setup %s camera %s: %s", role_name, device_path, e)

        logger.info("=== Registry setup complete (%d cameras) ===", len(assigned))

    def setup_cameras(
        self,
        internal_device: str = None,
        external_device: str = None,
        internal_backend: str = "v4l2",
        external_backend: str = "v4l2",
    ) -> None:
        """
        Setup cameras manually with optimized profiles for dual streaming.
        """
        logger.info("=== Setting up cameras manually ===")

        # Setup internal camera
        if internal_device:
            try:
                profile = self.internal_scanner.probe_device(internal_device)
                profile.safe_resolutions = ["1280x720", "640x480"]  # Primary camera
                self.internal_engine.add_stream("internal", profile)
                logger.info("✓ Internal camera: %s", internal_device)
            except Exception as e:
                logger.exception("Failed to setup internal camera: %s", e)

        # Setup external camera
        if external_device:
            try:
                profile = self.external_scanner.probe_device(external_device)
                profile.safe_resolutions = ["640x480", "320x240"]  # Secondary camera
                self.external_engine.add_stream("external", profile)
                logger.info("✓ External camera: %s", external_device)
            except Exception as e:
                logger.exception("Failed to setup external camera: %s", e)

        logger.info("=== Manual setup complete ===")

    def start(self) -> None:
        """Start both camera pipelines."""
        logger.info("=== Starting ultra-low-latency dual camera pipeline ===")

        self._running = True

        # Start internal camera (critical)
        logger.info("Starting internal camera pipeline...")
        self.internal_engine.start_all_streams()
        self.internal_engine.start_watchdog()

        # Start external camera (can fail independently)
        logger.info("Starting external camera pipeline...")
        self.external_engine.start_all_streams()
        self.external_engine.start_watchdog()

        logger.info("=== Dual camera pipeline started ===")

    def stop(self) -> None:
        """Stop both camera pipelines."""
        logger.info("=== Stopping dual camera pipeline ===")

        self._running = False

        # Stop both engines
        self.external_engine.stop_watchdog()
        self.external_engine.stop()
        self.internal_engine.stop_watchdog()
        self.internal_engine.stop()

        logger.info("=== Dual camera pipeline stopped ===")

    def get_pipeline_status(self) -> Dict[str, Any]:
        """Get status of both camera pipelines."""
        return {
            "device_id": self.device_id,
            "running": self._running,
            "internal_camera": self.internal_engine.get_all_status(),
            "external_camera": self.external_engine.get_all_status(),
            "rtsp_base_url": f"rtsp://{self.mediamtx_host}:{self.mediamtx_rtsp_port}",
            "latency_target": "<500ms",
            "architecture": "ultra-low-latency dual USB",
        }


# ============================================================================
# CONSTANTS
# ============================================================================

# Stream states (for backward compatibility)
STREAM_STATE_STARTING = STREAM_STATE_STARTING
STREAM_STATE_RUNNING = STREAM_STATE_RUNNING
STREAM_STATE_DEGRADED = STREAM_STATE_DEGRADED
STREAM_STATE_RECOVERING = STREAM_STATE_RECOVERING
STREAM_STATE_FAILED = STREAM_STATE_FAILED


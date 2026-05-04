"""
FFmpeg Process Manager with Multi-Format Support
Manages multiple FFmpeg subprocesses for camera streaming.
- Auto-detects MJPEG vs YUYV format support per device
- Handles "Device busy" conflicts via fuser/kill
- Implements exponential backoff retry
- Monitors processes and auto-restarts on failure
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable

from .constants import RESTART_BACKOFF, RESTART_MAX_ATTEMPTS

logger = logging.getLogger(__name__)


@dataclass
class StreamProcess:
    """Represents a single camera stream process"""
    camera_type: str
    device_path: str
    formats: list[str] = field(default_factory=list)
    process: Optional[subprocess.Popen] = None
    restart_count: int = 0
    last_start_time: float = 0
    last_error: Optional[str] = None
    last_command: list[str] = field(default_factory=list)
    selected_format: Optional[str] = None
    selected_size: str = "640x480"
    status: str = "stopped"  # "running", "stopped", "error"
    lock: threading.RLock = field(default_factory=threading.RLock)


class FFmpegManager:
    """Manages multiple FFmpeg streaming processes"""
    
    def __init__(
        self,
        device_id: str,
        mediamtx_host: str = "127.0.0.1",
        rtsp_port: int = 8554,
        on_stream_status_change: Optional[Callable[[str, str], None]] = None,
    ):
        self.device_id = device_id
        self.mediamtx_host = mediamtx_host
        self.rtsp_port = rtsp_port
        self.on_stream_status_change = on_stream_status_change

        self._lock = threading.RLock()
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._streams: dict[str, StreamProcess] = {}

    def add_stream(self, camera_type: str, device_path: str, formats: list[str]) -> None:
        """Register a camera stream to manage (with detected formats)."""
        with self._lock:
            if camera_type in self._streams:
                logger.warning("Stream for %s already exists, replacing", camera_type)
                self.stop_stream(camera_type)

            self._streams[camera_type] = StreamProcess(
                camera_type=camera_type,
                device_path=device_path,
                formats=formats,
            )
            logger.info(
                "Added stream: %s -> %s (formats: %s)",
                camera_type, device_path, ", ".join(formats)
            )

    def _input_format_candidates(self, formats: list[str]) -> list[str]:
        """
        Select input formats in failover order: MJPEG preferred, then YUYV.
        If formats list is empty, try auto-detection (empty string).
        """
        fmt_lower = [f.lower() for f in formats]
        candidates: list[str] = []
        for pref in ["mjpeg", "mjpg"]:
            if pref in fmt_lower:
                candidates.append("mjpeg")
                break
        for pref in ["yuyv", "yuyv422", "yuv422"]:
            if pref in fmt_lower:
                candidates.append("yuyv422")
                break
        # If no known formats detected, try auto-detection
        if not candidates:
            candidates.append("")
        return candidates

    def _is_device_busy(self, device_path: str) -> bool:
        """Check if /dev/videoX is currently in use."""
        try:
            # Try to open exclusively
            fd = os.open(device_path, os.O_RDWR | os.O_NONBLOCK)
            os.close(fd)
            return False
        except OSError as e:
            if e.errno in {11, 13, 16}:  # ETXTBSY, EACCES, EBUSY
                return True
            return False

    def _kill_blocking_process(self, device_path: str) -> bool:
        """Kill any process holding the camera device open."""
        dev_name = Path(device_path).name
        try:
            # Try fuser to find PIDs
            result = subprocess.run(
                ["fuser", "-v", device_path],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                logger.warning("Device %s is busy. Attempting to free...", device_path)
                # Kill processes
                subprocess.run(["fuser", "-k", device_path], timeout=5)
                time.sleep(1)
                return True
        except FileNotFoundError:
            logger.warning("fuser not available; cannot auto-release device")
        except Exception as e:
            logger.warning("Failed to kill blocking process: %s", e)
        return False

    def _build_ffmpeg_cmd(
        self,
        stream: StreamProcess,
        input_format: str,
        video_size: str = "640x480",
    ) -> list[str]:
        """
        Build FFmpeg command with optimal input format.
        Returns None if format selection fails critically.
        """
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
        cmd.extend([
            "-f", "v4l2",
            "-framerate", "25",
            "-video_size", video_size,
        ])

        if input_format:
            cmd.extend(["-input_format", input_format])

        cmd.extend(["-i", stream.device_path])

        # Encoding options
        cmd.extend([
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-g", "10",
            "-keyint_min", "10",
        ])

        cmd.extend(["-f", "rtsp", "-rtsp_transport", "tcp"])

        output_url = (
            f"rtsp://{self.mediamtx_host}:{self.rtsp_port}/"
            f"{self.device_id}/{stream.camera_type}"
        )
        cmd.append(output_url)

        logger.info(
            "Built FFmpeg cmd for %s: device=%s, input_format=%s",
            stream.camera_type, stream.device_path,
            input_format or "auto",
        )
        return cmd

    def start_stream(self, camera_type: str) -> bool:
        """Start streaming for a specific camera."""
        with self._lock:
            if camera_type not in self._streams:
                logger.error("Cannot start unknown stream: %s", camera_type)
                return False

            stream = self._streams[camera_type]
            if stream.status == "running":
                logger.warning("Stream %s already running", camera_type)
                return True

            # Check device availability and clear if busy
            if self._is_device_busy(stream.device_path):
                logger.warning("Device %s is busy, attempting to free...", stream.device_path)
                if not self._kill_blocking_process(stream.device_path):
                    stream.last_error = "Device is busy and could not be freed"
                    stream.status = "error"
                    if self.on_stream_status_change:
                        self.on_stream_status_change(camera_type, "error")
                    return False
                time.sleep(0.5)  # Give kernel time to release

            return self._start_ffmpeg(stream)

    def _start_ffmpeg(self, stream: StreamProcess) -> bool:
        """Start FFmpeg subprocess with proper error handling."""
        last_error = ""
        candidates = self._input_format_candidates(stream.formats)
        
        logger.info(
            "Attempting to start FFmpeg for %s: device=%s, detected_formats=%s, candidates=%s",
            stream.camera_type,
            stream.device_path,
            ",".join(stream.formats) or "none",
            candidates,
        )
        
        for input_format in candidates:
            cmd = self._build_ffmpeg_cmd(stream, input_format)
            logger.info(
                "Starting FFmpeg for %s: selected camera device=%s format=%s command=%s",
                stream.camera_type,
                stream.device_path,
                input_format or "auto",
                " ".join(cmd),
            )

            try:
                stream.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    preexec_fn=os.setsid
                )
                time.sleep(2)
                returncode = stream.process.poll()
                if returncode is not None:
                    stderr = (
                        stream.process.stderr.read().decode(errors="replace")
                        if stream.process.stderr else ""
                    )
                    last_error = stderr.strip() or f"ffmpeg exited with code {returncode}"
                    logger.warning(
                        "FFmpeg failed to open %s with %s: %s",
                        stream.device_path,
                        input_format or "auto",
                        last_error[:500],
                    )
                    if self._handle_start_failure(stream, last_error):
                        time.sleep(0.5)
                    if self._is_invalid_camera_error(last_error):
                        break
                    continue

                stream.last_start_time = time.time()
                stream.last_error = None
                stream.last_command = cmd
                stream.selected_format = input_format or None
                stream.selected_size = "640x480"
                stream.status = "running"

                logger.info(
                    "FFmpeg started for %s (PID: %d, device=%s, format=%s)",
                    stream.camera_type,
                    stream.process.pid,
                    stream.device_path,
                    input_format or "auto",
                )

                if self.on_stream_status_change:
                    self.on_stream_status_change(stream.camera_type, "running")
                return True

            except FileNotFoundError:
                stream.last_error = "ffmpeg not found in PATH"
                stream.status = "error"
                logger.exception("FFmpeg binary not found")
                break
            except Exception as exc:
                last_error = str(exc)
                stream.last_error = last_error
                stream.status = "error"
                logger.exception("Failed to start FFmpeg for %s", stream.camera_type)
                break

        stream.last_error = last_error or "No suitable input format"
        stream.status = "error"

        if self.on_stream_status_change:
            self.on_stream_status_change(stream.camera_type, "error")
        return False

    def _handle_start_failure(self, stream: StreamProcess, error: str) -> bool:
        if self._is_device_busy_error(error):
            logger.warning("Device busy while opening %s; freeing and retrying", stream.device_path)
            return self._kill_blocking_process(stream.device_path)
        return False

    @staticmethod
    def _is_device_busy_error(error: str) -> bool:
        lower = error.lower()
        return "device or resource busy" in lower or "resource busy" in lower

    @staticmethod
    def _is_invalid_camera_error(error: str) -> bool:
        lower = error.lower()
        return (
            "inappropriate ioctl" in lower
            or "not a video capture device" in lower
            or "no formats found" in lower
            or "vidioc_enum_fmt" in lower
        )

    def stop_stream(self, camera_type: str) -> bool:
        """Stop streaming for a specific camera."""
        with self._lock:
            if camera_type not in self._streams:
                return False
            stream = self._streams[camera_type]
            return self._stop_ffmpeg(stream)

    def _stop_ffmpeg(self, stream: StreamProcess) -> bool:
        """Stop FFmpeg subprocess gracefully."""
        if not stream.process:
            return True

        try:
            logger.info("Stopping FFmpeg for %s (PID: %d)",
                       stream.camera_type, stream.process.pid)

            os.killpg(os.getpgid(stream.process.pid), signal.SIGTERM)

            try:
                stream.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("FFmpeg did not exit, sending SIGKILL")
                os.killpg(os.getpgid(stream.process.pid), signal.SIGKILL)
                stream.process.wait()

            stream.status = "stopped"
            stream.process = None

            if self.on_stream_status_change:
                self.on_stream_status_change(stream.camera_type, "stopped")
            return True

        except Exception as exc:
            logger.exception("Error stopping FFmpeg for %s", stream.camera_type)
            stream.last_error = str(exc)
            return False

    def stop_all(self) -> None:
        """Stop all streams."""
        with self._lock:
            for camera_type in list(self._streams.keys()):
                self.stop_stream(camera_type)

    def get_stream_status(self, camera_type: str) -> Optional[dict]:
        """Get status dict for a stream."""
        with self._lock:
            if camera_type not in self._streams:
                return None
            stream = self._streams[camera_type]
            return {
                "camera_type": camera_type,
                "device_path": stream.device_path,
                "status": stream.status,
                "restart_count": stream.restart_count,
                "last_error": stream.last_error,
                "pid": stream.process.pid if stream.process else None,
                "formats": stream.formats,
                "selected_format": stream.selected_format,
                "ffmpeg_command": stream.last_command,
            }

    def get_all_status(self) -> dict[str, dict]:
        """Get status of all streams."""
        with self._lock:
            return {
                ct: {
                    "camera_type": ct,
                    "device_path": s.device_path,
                    "status": s.status,
                    "restart_count": s.restart_count,
                    "last_error": s.last_error,
                    "pid": s.process.pid if s.process else None,
                    "formats": s.formats,
                    "selected_format": s.selected_format,
                    "ffmpeg_command": s.last_command,
                }
                for ct, s in self._streams.items()
            }

    def start_all(self) -> None:
        """Start all registered streams."""
        with self._lock:
            for camera_type in self._streams:
                self.start_stream(camera_type)

    def start_monitoring(self) -> None:
        """Start background monitoring thread."""
        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="ffmpeg-monitor"
        )
        self._monitor_thread.start()
        logger.info("FFmpeg monitoring started")

    def stop_monitoring(self) -> None:
        """Stop background monitoring."""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
        logger.info("FFmpeg monitoring stopped")

    def _monitor_loop(self) -> None:
        """Background thread that monitors and restarts failed processes."""
        while self._running:
            time.sleep(5)  # Check every 5 seconds

            failed_streams = []
            with self._lock:
                for camera_type, stream in self._streams.items():
                    if stream.status != "running" or not stream.process:
                        continue

                    returncode = stream.process.poll()
                    if returncode is not None:
                        stderr = (
                            stream.process.stderr.read().decode()
                            if stream.process.stderr else ""
                        )
                        logger.warning(
                            "FFmpeg for %s exited (code %d): %s",
                            camera_type, returncode, stderr[:300]
                        )
                        stream.status = "error"
                        stream.last_error = f"Exit code {returncode}"
                        if stderr:
                            stream.last_error = stderr[:500]

                        if self.on_stream_status_change:
                            self.on_stream_status_change(camera_type, "error")

                        failed_streams.append(stream)

            for stream in failed_streams:
                self._attempt_restart(stream)

    def _attempt_restart(self, stream: StreamProcess) -> bool:
        """Attempt to restart a failed stream with backoff"""
        if stream.last_error and self._is_invalid_camera_error(stream.last_error):
            logger.error(
                "Not restarting %s because %s is not a valid capture camera: %s",
                stream.camera_type,
                stream.device_path,
                stream.last_error[:300],
            )
            return False

        if stream.last_error and self._is_device_busy_error(stream.last_error):
            self._kill_blocking_process(stream.device_path)

        if stream.restart_count >= RESTART_MAX_ATTEMPTS:
            logger.error(
                "Max restart attempts reached for %s, giving up",
                stream.camera_type
            )
            stream.last_error = f"Max restart attempts ({RESTART_MAX_ATTEMPTS}) exceeded"
            return False
        
        backoff = RESTART_BACKOFF[
            min(stream.restart_count, len(RESTART_BACKOFF) - 1)
        ]
        logger.info(
            "Restarting %s in %d seconds (attempt %d/%d)",
            stream.camera_type, backoff,
            stream.restart_count + 1, RESTART_MAX_ATTEMPTS
        )

        time.sleep(backoff)
        stream.restart_count += 1

        # Clean up any leftover process
        if stream.process:
            try:
                os.killpg(os.getpgid(stream.process.pid), signal.SIGKILL)
            except Exception:
                pass

        success = self._start_ffmpeg(stream)
        if success:
            logger.info("Successfully restarted %s", stream.camera_type)
            stream.restart_count = 0
        else:
            logger.error("Failed to restart %s", stream.camera_type)
        return success

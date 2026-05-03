"""
FFmpeg Process Manager
Manages multiple FFmpeg subprocesses for camera streaming.
- Starts/stops streams
- Monitors health and auto-restarts on failure
- Uses low-latency encoding optimized for Raspberry Pi 4
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

from .constants import (
    FFMPEG_INPUT_OPTIONS,
    FFMPEG_ENCODE_VIDEO,
    FFMPEG_OUTPUT_OPTIONS,
    PROCESS_CHECK_INTERVAL,
    RESTART_MAX_ATTEMPTS,
    RESTART_BACKOFF,
)

logger = logging.getLogger(__name__)


@dataclass
class StreamProcess:
    """Represents a single camera stream process"""
    camera_type: str
    device_path: str
    process: Optional[subprocess.Popen] = None
    restart_count: int = 0
    last_start_time: float = 0
    last_error: Optional[str] = None
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
    
    def add_stream(self, camera_type: str, device_path: str) -> None:
        """Register a camera stream to manage"""
        with self._lock:
            if camera_type in self._streams:
                logger.warning("Stream for %s already exists, replacing", camera_type)
                self.stop_stream(camera_type)
            
            self._streams[camera_type] = StreamProcess(
                camera_type=camera_type,
                device_path=device_path
            )
            logger.info("Added stream: %s -> %s", camera_type, device_path)
    
    def start_stream(self, camera_type: str) -> bool:
        """Start streaming for a specific camera"""
        with self._lock:
            if camera_type not in self._streams:
                logger.error("Cannot start unknown stream: %s", camera_type)
                return False
            
            stream = self._streams[camera_type]
            if stream.status == "running":
                logger.warning("Stream %s already running", camera_type)
                return True
            
            return self._start_ffmpeg(stream)
    
    def stop_stream(self, camera_type: str) -> bool:
        """Stop streaming for a specific camera"""
        with self._lock:
            if camera_type not in self._streams:
                return False
            
            stream = self._streams[camera_type]
            return self._stop_ffmpeg(stream)
    
    def stop_all(self) -> None:
        """Stop all streams"""
        with self._lock:
            for camera_type in list(self._streams.keys()):
                self.stop_stream(camera_type)
    
    def get_stream_status(self, camera_type: str) -> Optional[dict]:
        """Get status dict for a stream"""
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
            }
    
    def get_all_status(self) -> dict[str, dict]:
        """Get status of all streams"""
        with self._lock:
            return {
                ct: {
                    "camera_type": ct,
                    "device_path": s.device_path,
                    "status": s.status,
                    "restart_count": s.restart_count,
                    "last_error": s.last_error,
                    "pid": s.process.pid if s.process else None,
                }
                for ct, s in self._streams.items()
            }
    
    def start_all(self) -> None:
        """Start all registered streams"""
        with self._lock:
            for camera_type in self._streams:
                self.start_stream(camera_type)
    
    def start_monitoring(self) -> None:
        """Start background monitoring thread"""
        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="ffmpeg-monitor"
        )
        self._monitor_thread.start()
        logger.info("FFmpeg monitoring started")
    
    def stop_monitoring(self) -> None:
        """Stop background monitoring"""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
        logger.info("FFmpeg monitoring stopped")
    
    def _build_ffmpeg_cmd(self, stream: StreamProcess) -> list[str]:
        """Build FFmpeg command for a given stream"""
        # FFmpeg input options that must come before -i
        input_options = [
            "-f", "v4l2",
            "-input_format", "mjpeg",  # Many Pi cameras output MJPEG; adapt if needed
        ]
        
        # Build output URL: rtsp://host:port/device_id/stream_type
        output_url = f"rtsp://{self.mediamtx_host}:{self.rtsp_port}/{self.device_id}/{stream.camera_type}"
        
        cmd = (
            ["ffmpeg", "-hide_banner", "-loglevel", "warning"] +
            FFMPEG_INPUT_OPTIONS +   # global flags like -fflags nobuffer
            input_options +           # input format + device
            ["-i", stream.device_path] +
            FFMPEG_ENCODE_VIDEO +
            FFMPEG_OUTPUT_OPTIONS +
            [output_url]
        )
        return cmd
    
    def _start_ffmpeg(self, stream: StreamProcess) -> bool:
        """Start FFmpeg subprocess for a stream"""
        cmd = self._build_ffmpeg_cmd(stream)
        logger.info("Starting FFmpeg for %s: %s", stream.camera_type, " ".join(cmd))
        
        try:
            # Start subprocess with pipes for stdout/stderr
            stream.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                preexec_fn=os.setsid  # So we can kill the whole process group
            )
            stream.last_start_time = time.time()
            stream.last_error = None
            stream.restart_count = 0
            stream.status = "running"
            
            logger.info(
                "FFmpeg started for %s (PID: %d)",
                stream.camera_type,
                stream.process.pid
            )
            
            if self.on_stream_status_change:
                self.on_stream_status_change(stream.camera_type, "running")
            
            return True
        
        except Exception as exc:
            stream.last_error = str(exc)
            stream.status = "error"
            logger.exception("Failed to start FFmpeg for %s", stream.camera_type)
            if self.on_stream_status_change:
                self.on_stream_status_change(stream.camera_type, "error")
            return False
    
    def _stop_ffmpeg(self, stream: StreamProcess) -> bool:
        """Stop FFmpeg subprocess gracefully"""
        if not stream.process:
            return True
        
        try:
            logger.info("Stopping FFmpeg for %s (PID: %d)", stream.camera_type, stream.process.pid)
            
            # Send SIGTERM to process group
            os.killpg(os.getpgid(stream.process.pid), signal.SIGTERM)
            
            # Wait for graceful shutdown
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
    
    def _monitor_loop(self) -> None:
        """Background thread that monitors and restarts failed processes"""
        while self._running:
            time.sleep(PROCESS_CHECK_INTERVAL)
            
            with self._lock:
                for camera_type, stream in self._streams.items():
                    if stream.status != "running" or not stream.process:
                        continue
                    
                    # Check if process is still alive
                    returncode = stream.process.poll()
                    if returncode is not None:
                        # Process exited
                        stderr = stream.process.stderr.read().decode() if stream.process.stderr else ""
                        logger.warning(
                            "FFmpeg for %s exited with code %d. stderr: %s",
                            camera_type,
                            returncode,
                            stderr[:200]
                        )
                        
                        stream.status = "error"
                        stream.last_error = f"Process exited with code {returncode}"
                        
                        if self.on_stream_status_change:
                            self.on_stream_status_change(camera_type, "error")
                        
                        # Attempt restart
                        self._attempt_restart(stream)
    
    def _attempt_restart(self, stream: StreamProcess) -> bool:
        """Attempt to restart a failed stream with backoff"""
        if stream.restart_count >= RESTART_MAX_ATTEMPTS:
            logger.error(
                "Max restart attempts reached for %s, giving up",
                stream.camera_type
            )
            stream.last_error = "Max restart attempts exceeded"
            return False
        
        backoff = RESTART_BACKOFF[min(stream.restart_count, len(RESTART_BACKOFF) - 1)]
        logger.info(
            "Restarting %s in %d seconds (attempt %d/%d)",
            stream.camera_type,
            backoff,
            stream.restart_count + 1,
            RESTART_MAX_ATTEMPTS
        )
        
        time.sleep(backoff)
        stream.restart_count += 1
        
        # Ensure any leftover process is cleaned
        if stream.process:
            try:
                os.killpg(os.getpgid(stream.process.pid), signal.SIGKILL)
            except Exception:
                pass
        
        success = self._start_ffmpeg(stream)
        if success:
            logger.info("Successfully restarted %s", stream.camera_type)
        else:
            logger.error("Failed to restart %s", stream.camera_type)
        
        return success

"""
Stream Worker - FFmpeg Supervisor

One worker per camera. Responsible for:
- Starting FFmpeg process
- Publishing RTSP stream to MediaMTX
- Monitoring PID health
- Automatic restart on failure
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from typing import Optional, Callable
from pathlib import Path

from .models import CameraConfig, StreamHealth

logger = logging.getLogger(__name__)


class StreamWorker:
    """
    Supervises a single camera stream.
    
    Manages FFmpeg process lifecycle, monitors health,
    and automatically restarts on failure.
    """
    
    # Restart configuration
    MAX_RESTARTS = 3
    RESTART_DELAY = 2  # seconds
    HEALTH_CHECK_INTERVAL = 5  # seconds
    
    def __init__(
        self,
        config: CameraConfig,
        mediamtx_host: str = "127.0.0.1",
        mediamtx_port: int = 8554,
        device_id: str = "unknown",
        on_health_change: Optional[Callable[[StreamHealth], None]] = None,
    ):
        self.config = config
        self.mediamtx_host = mediamtx_host
        self.mediamtx_port = mediamtx_port
        self.device_id = device_id
        self.on_health_change = on_health_change
        
        # Process state
        self.process: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        
        # Health tracking
        self.health = StreamHealth(
            camera_type=config.camera_type,
            device=config.device,
            format=config.format,
            resolution=config.resolution,
        )
        
        # Statistics
        self._start_time: Optional[float] = None
        self._frame_count = 0
        self._last_frame_time: Optional[float] = None
        
        logger.info(
            "StreamWorker created for %s (%s)",
            config.camera_type, config.device,
        )
    
    def start(self) -> bool:
        """
        Start the stream worker.
        
        Returns:
            True if started successfully
        """
        with self._lock:
            if self._running:
                logger.warning("Stream worker already running for %s", self.config.device)
                return True
            
            # Start FFmpeg process
            if not self._start_ffmpeg():
                return False
            
            # Start health monitor
            self._running = True
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
                name=f"stream-worker-{self.config.camera_type}",
            )
            self._monitor_thread.start()
            
            logger.info(
                "Stream worker started for %s (PID: %s)",
                self.config.device, self.process.pid if self.process else None,
            )
            
            return True
    
    def stop(self) -> None:
        """Stop the stream worker."""
        with self._lock:
            if not self._running:
                return
            
            logger.info("Stopping stream worker for %s", self.config.device)
            self._running = False
            
            # Stop monitor thread
            if self._monitor_thread:
                self._monitor_thread.join(timeout=5)
                self._monitor_thread = None
            
            # Stop FFmpeg process
            self._stop_ffmpeg()
            
            # Update health
            self.health.state = "stopped"
            self._notify_health_change()
            
            logger.info("Stream worker stopped for %s", self.config.device)
    
    def _start_ffmpeg(self) -> bool:
        """
        Start FFmpeg process.
        
        Returns:
            True if started successfully
        """
        with self._lock:
            # Build FFmpeg command
            cmd = self._build_ffmpeg_command()
            
            logger.info(
                "Starting FFmpeg for %s: %s",
                self.config.camera_type, " ".join(cmd),
            )
            
            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    preexec_fn=os.setsid,
                )
                
                # Wait briefly to check if process starts
                time.sleep(1)
                
                # Check if process is still running
                if self.process.poll() is not None:
                    # Process exited immediately
                    stderr = self._read_stderr()
                    logger.error(
                        "FFmpeg failed to start for %s: %s",
                        self.config.camera_type, stderr[:500],
                    )
                    self.health.state = "failed"
                    self.health.last_error = stderr[:500]
                    self._notify_health_change()
                    return False
                
                # Process started successfully
                self._start_time = time.time()
                self.health.state = "running"
                self.health.pid = self.process.pid
                self.health.restart_count = 0
                self._frame_count = 0
                self._last_frame_time = time.time()
                
                logger.info(
                    "FFmpeg started for %s (PID: %d, format: %s, resolution: %s)",
                    self.config.camera_type,
                    self.process.pid,
                    self.config.format,
                    self.config.resolution,
                )
                
                self._notify_health_change()
                return True
                
            except FileNotFoundError:
                logger.exception("FFmpeg not found in PATH")
                self.health.state = "failed"
                self.health.last_error = "FFmpeg not found"
                self._notify_health_change()
                return False
            except Exception as e:
                logger.exception("Failed to start FFmpeg for %s", self.config.camera_type)
                self.health.state = "failed"
                self.health.last_error = str(e)
                self._notify_health_change()
                return False
    
    def _build_ffmpeg_command(self) -> list[str]:
        """
        Build FFmpeg command for this camera.
        
        Returns:
            List of command arguments
        """
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-f", "v4l2",
            "-framerate", "25",
            "-video_size", self.config.resolution,
        ]
        
        # Add input format if not auto
        if self.config.format != "auto":
            cmd.extend(["-input_format", self.config.format])
        
        cmd.extend(["-i", self.config.device])
        
        # Encoding options
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
        stream_name = f"{self.device_id}/{self.config.camera_type}"
        cmd.extend([
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            f"rtsp://{self.mediamtx_host}:{self.mediamtx_port}/{stream_name}",
        ])
        
        return cmd
    
    def _stop_ffmpeg(self) -> None:
        """Stop FFmpeg process."""
        with self._lock:
            if not self.process:
                return
            
            try:
                logger.info(
                    "Stopping FFmpeg for %s (PID: %d)",
                    self.config.camera_type, self.process.pid,
                )
                
                # Send SIGTERM to process group
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                
                # Wait for graceful shutdown
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("FFmpeg did not exit, sending SIGKILL")
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                    self.process.wait()
                
            except ProcessLookupError:
                # Process already dead
                pass
            except Exception as e:
                logger.exception("Error stopping FFmpeg for %s", self.config.camera_type)
            
            finally:
                self.process = None
                self._start_time = None
    
    def _monitor_loop(self) -> None:
        """
        Monitor loop - checks process health periodically.
        """
        while self._running:
            time.sleep(self.HEALTH_CHECK_INTERVAL)
            
            if not self._running:
                break
            
            self._check_health()
    
    def _check_health(self) -> None:
        """
        Check process health and restart if necessary.
        """
        with self._lock:
            if not self._running or not self.process:
                return
            
            # Check if process is still alive
            returncode = self.process.poll()
            
            if returncode is not None:
                # Process died
                stderr = self._read_stderr()
                
                logger.warning(
                    "FFmpeg process died for %s (code: %d): %s",
                    self.config.camera_type, returncode, stderr[:300],
                )
                
                self.health.state = "restarting"
                self.health.last_error = stderr[:500]
                self._notify_health_change()
                
                # Restart
                self._restart()
                return
            
            # Update uptime
            if self._start_time:
                self.health.uptime_seconds = time.time() - self._start_time
            
            # Update FPS estimate
            self._update_fps()
    
    def _restart(self) -> None:
        """
        Restart the stream.
        """
        with self._lock:
            if self.health.restart_count >= self.MAX_RESTARTS:
                logger.error(
                    "Max restarts (%d) exceeded for %s",
                    self.MAX_RESTARTS, self.config.camera_type,
                )
                self.health.state = "failed"
                self._notify_health_change()
                return
            
            # Increment restart count
            self.health.restart_count += 1
            restart_num = self.health.restart_count
            
            logger.info(
                "Restarting stream for %s (attempt %d/%d)",
                self.config.camera_type, restart_num, self.MAX_RESTARTS,
            )
            
            # Stop current process
            self._stop_ffmpeg()
            
            # Wait before restart
            time.sleep(self.RESTART_DELAY)
            
            # Try to restart
            if self._start_ffmpeg():
                logger.info(
                    "Stream restarted successfully for %s",
                    self.config.camera_type,
                )
            else:
                logger.error(
                    "Stream restart failed for %s",
                    self.config.camera_type,
                )
    
    def _read_stderr(self) -> str:
        """
        Read stderr from FFmpeg process.
        
        Returns:
            Stderr output as string
        """
        if not self.process or not self.process.stderr:
            return ""
        
        try:
            return self.process.stderr.read().decode(errors="replace")
        except Exception:
            return ""
    
    def _update_fps(self) -> None:
        """
        Update FPS estimate.
        """
        now = time.time()
        
        # Simple frame counter - increment periodically
        # In production, you'd parse FFmpeg output for actual frame count
        if self.health.state == "running":
            self._frame_count += 1
            
            if self._last_frame_time:
                elapsed = now - self._last_frame_time
                if elapsed > 0:
                    self.health.fps = round(1.0 / elapsed, 1)
            
            self._last_frame_time = now
    
    def _notify_health_change(self) -> None:
        """Notify health change callback."""
        if self.on_health_change:
            try:
                self.on_health_change(self.health)
            except Exception as e:
                logger.exception("Health change callback failed: %s", e)
    
    @property
    def is_running(self) -> bool:
        """Check if worker is running."""
        with self._lock:
            return self._running and self.process is not None
    
    @property
    def get_health(self) -> StreamHealth:
        """Get current health status."""
        with self._lock:
            # Update uptime
            if self._start_time and self.health.state == "running":
                self.health.uptime_seconds = time.time() - self._start_time
            
            return self.health

"""
Stream Watchdog - Health Monitoring and Auto-Recovery

Monitors all stream workers and triggers recovery on failure.
Checks every 10 seconds:
- FFmpeg process alive
- RTSP stream reachable (ffprobe)
- Frame heartbeat counter

If failure detected:
- Restart ONLY affected camera stream
- Do NOT restart whole system
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Dict, Optional, Callable

from .stream_worker import StreamWorker
from .models import StreamHealth

logger = logging.getLogger(__name__)


class StreamWatchdog:
    """
    Monitors stream workers and triggers auto-recovery.
    
    Runs a background loop that checks stream health every 10 seconds.
    If a stream is unhealthy, restarts only that stream.
    """
    
    # Check interval
    CHECK_INTERVAL = 10  # seconds
    
    # RTSP reachability timeout
    RTSP_CHECK_TIMEOUT = 5  # seconds
    
    # Frame stall threshold (seconds without frames)
    FRAME_STALL_THRESHOLD = 30  # seconds
    
    def __init__(
        self,
        on_recovery: Optional[Callable[[str], None]] = None,
    ):
        self.workers: Dict[str, StreamWorker] = {}
        self.on_recovery = on_recovery
        
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        
        logger.info("StreamWatchdog initialized")
    
    def add_worker(self, worker: StreamWorker) -> None:
        """
        Add a stream worker to monitor.
        
        Args:
            worker: StreamWorker to monitor
        """
        with self._lock:
            camera_type = worker.config.camera_type
            self.workers[camera_type] = worker
            logger.info("Added worker for %s to watchdog", camera_type)
    
    def remove_worker(self, camera_type: str) -> None:
        """
        Remove a stream worker from monitoring.
        
        Args:
            camera_type: Camera type to remove
        """
        with self._lock:
            if camera_type in self.workers:
                del self.workers[camera_type]
                logger.info("Removed worker for %s from watchdog", camera_type)
    
    def start(self) -> None:
        """Start the watchdog monitoring loop."""
        with self._lock:
            if self._running:
                logger.warning("Watchdog already running")
                return
            
            self._running = True
            self._thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
                name="stream-watchdog",
            )
            self._thread.start()
            
            logger.info("StreamWatchdog started (check interval: %ds)", self.CHECK_INTERVAL)
    
    def stop(self) -> None:
        """Stop the watchdog monitoring loop."""
        with self._lock:
            if not self._running:
                return
            
            self._running = False
            
            if self._thread:
                self._thread.join(timeout=5)
                self._thread = None
            
            logger.info("StreamWatchdog stopped")
    
    def _monitor_loop(self) -> None:
        """
        Main monitoring loop.
        Checks all streams periodically and triggers recovery if needed.
        """
        while self._running:
            time.sleep(self.CHECK_INTERVAL)
            
            if not self._running:
                break
            
            self._check_all_streams()
    
    def _check_all_streams(self) -> None:
        """
        Check health of all monitored streams.
        """
        with self._lock:
            workers = list(self.workers.values())
        
        for worker in workers:
            self._check_stream(worker)
    
    def _check_stream(self, worker: StreamWorker) -> None:
        """
        Check health of a single stream.
        
        Args:
            worker: StreamWorker to check
        """
        camera_type = worker.config.camera_type
        health = worker.get_health
        
        # Check 1: Process alive
        if not worker.is_running:
            logger.warning(
                "Stream %s is not running (state: %s)",
                camera_type, health.state,
            )
            self._trigger_recovery(worker, "process_not_running")
            return
        
        # Check 2: Process died
        if health.state == "failed":
            logger.warning(
                "Stream %s is in failed state: %s",
                camera_type, health.last_error,
            )
            self._trigger_recovery(worker, "stream_failed")
            return
        
        # Check 3: RTSP reachability (only for running streams)
        if health.state == "running":
            if not self._check_rtsp_reachable(worker):
                logger.warning(
                    "RTSP stream for %s is not reachable",
                    camera_type,
                )
                self._trigger_recovery(worker, "rtsp_unreachable")
                return
        
        # Check 4: Frame stall
        if health.state == "running":
            if self._is_frame_stalled(worker):
                logger.warning(
                    "Stream %s appears stalled (no frames for %.0fs)",
                    camera_type, self.FRAME_STALL_THRESHOLD,
                )
                self._trigger_recovery(worker, "frame_stall")
                return
        
        # Stream is healthy
        logger.debug("Stream %s is healthy (uptime: %.0fs, fps: %.1f)",
                    camera_type, health.uptime_seconds, health.fps)
    
    def _check_rtsp_reachable(self, worker: StreamWorker) -> bool:
        """
        Check if RTSP stream is reachable using ffprobe.
        
        Args:
            worker: StreamWorker to check
        
        Returns:
            True if stream is reachable
        """
        camera_type = worker.config.camera_type
        stream_url = (
            f"rtsp://{worker.mediamtx_host}:{worker.mediamtx_port}/"
            f"{worker.device_id}/{camera_type}"
        )
        
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-timeout", str(self.RTSP_CHECK_TIMEOUT * 1000000),
                    "-rtsp_transport", "tcp",
                    "-show_entries", "stream=codec_type",
                    "-of", "json",
                    stream_url,
                ],
                capture_output=True,
                text=True,
                timeout=self.RTSP_CHECK_TIMEOUT + 2,
            )
            
            # If ffprobe succeeds, stream is reachable
            return result.returncode == 0
            
        except subprocess.TimeoutExpired:
            logger.debug("RTSP check timed out for %s", camera_type)
            return False
        except FileNotFoundError:
            # ffprobe not available, skip check
            logger.debug("ffprobe not available, skipping RTSP check")
            return True  # Assume healthy if can't check
        except Exception as e:
            logger.debug("RTSP check failed for %s: %s", camera_type, e)
            return False
    
    def _is_frame_stalled(self, worker: StreamWorker) -> bool:
        """
        Check if stream has stalled (no frames for threshold period).
        
        Args:
            worker: StreamWorker to check
        
        Returns:
            True if stream appears stalled
        """
        health = worker.get_health
        
        # If FPS is 0 and uptime is significant, stream might be stalled
        if health.uptime_seconds > self.FRAME_STALL_THRESHOLD:
            if health.fps == 0:
                return True
        
        return False
    
    def _trigger_recovery(self, worker: StreamWorker, reason: str) -> None:
        """
        Trigger recovery for a failed stream.
        
        Args:
            worker: StreamWorker to recover
            reason: Reason for recovery
        """
        camera_type = worker.config.camera_type
        
        logger.info(
            "Triggering recovery for %s (reason: %s)",
            camera_type, reason,
        )
        
        # Update health state
        health = worker.get_health
        health.state = "recovering"
        health.last_error = reason
        worker._notify_health_change()
        
        # Stop the worker
        worker.stop()
        
        # Wait briefly
        time.sleep(1)
        
        # Restart
        if worker.start():
            logger.info(
                "Recovery successful for %s",
                camera_type,
            )
            
            if self.on_recovery:
                try:
                    self.on_recovery(camera_type)
                except Exception as e:
                    logger.exception("Recovery callback failed: %s", e)
        else:
            logger.error(
                "Recovery failed for %s",
                camera_type,
            )
    
    def get_status(self) -> Dict[str, dict]:
        """
        Get health status of all monitored streams.
        
        Returns:
            Dictionary mapping camera type to health dict
        """
        with self._lock:
            return {
                ct: worker.get_health.to_dict()
                for ct, worker in self.workers.items()
            }
    
    def is_running(self) -> bool:
        """Check if watchdog is running."""
        with self._lock:
            return self._running

"""
Watchdog Module
Monitors pipeline health and handles automatic recovery.
"""

from __future__ import annotations
import logging
import time
import threading
from typing import Dict, Callable, Optional

from .pipelines.base_pipeline import BasePipeline, PipelineStatus

logger = logging.getLogger(__name__)


class PipelineWatchdog:
    """Watchdog for monitoring and recovering streaming pipelines"""

    CHECK_INTERVAL = 5.0  # seconds
    HEALTH_TIMEOUT = 30.0  # seconds without frames = unhealthy
    MAX_RESTART_ATTEMPTS = 3
    RESTART_BACKOFF_BASE = 5.0  # seconds

    def __init__(self):
        self.pipelines: Dict[str, BasePipeline] = {}
        self.restart_counts: Dict[str, int] = {}
        self.last_restart_times: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._health_callbacks: list[Callable[[str, bool, str], None]] = []

    def add_pipeline(self, stream_name: str, pipeline: BasePipeline) -> None:
        """Add a pipeline to be monitored"""
        with self._lock:
            self.pipelines[stream_name] = pipeline
            self.restart_counts[stream_name] = 0
            logger.info(f"Added pipeline {stream_name} to watchdog")

    def remove_pipeline(self, stream_name: str) -> None:
        """Remove a pipeline from monitoring"""
        with self._lock:
            if stream_name in self.pipelines:
                del self.pipelines[stream_name]
                self.restart_counts.pop(stream_name, None)
                self.last_restart_times.pop(stream_name, None)
                logger.info(f"Removed pipeline {stream_name} from watchdog")

    def add_health_callback(self, callback: Callable[[str, bool, str], None]) -> None:
        """
        Add callback for health changes.
        Callback signature: (stream_name, is_healthy, reason)
        """
        with self._lock:
            self._health_callbacks.append(callback)

    def start(self) -> None:
        """Start the watchdog monitoring"""
        with self._lock:
            if self._running:
                return

            self._running = True
            self._thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
                name="pipeline-watchdog"
            )
            self._thread.start()
            logger.info("Pipeline watchdog started")

    def stop(self) -> None:
        """Stop the watchdog monitoring"""
        with self._lock:
            self._running = False

        if self._thread:
            self._thread.join(timeout=5)
            logger.info("Pipeline watchdog stopped")

    def _monitor_loop(self) -> None:
        """Main monitoring loop"""
        while self._running:
            try:
                self._check_all_pipelines()
            except Exception as e:
                logger.exception(f"Error in watchdog monitor loop: {e}")

            time.sleep(self.CHECK_INTERVAL)

    def _check_all_pipelines(self) -> None:
        """Check health of all monitored pipelines"""
        with self._lock:
            stream_names = list(self.pipelines.keys())

        for stream_name in stream_names:
            try:
                self._check_pipeline(stream_name)
            except Exception as e:
                logger.exception(f"Error checking pipeline {stream_name}: {e}")

    def _check_pipeline(self, stream_name: str) -> None:
        """Check health of a specific pipeline"""
        with self._lock:
            pipeline = self.pipelines.get(stream_name)
            if not pipeline:
                return

        # Get current status
        status = pipeline.get_status()

        # Check if pipeline should be running but isn't
        if not status.is_running:
            logger.warning(f"Pipeline {stream_name} is not running")
            self._handle_pipeline_failure(stream_name, pipeline, "pipeline_not_running")
            return

        # Check for process health
        if not pipeline.is_healthy():
            logger.warning(f"Pipeline {stream_name} process is unhealthy")
            self._handle_pipeline_failure(stream_name, pipeline, "process_unhealthy")
            return

        # Check for frame timeout (if we had frame monitoring)
        if status.last_frame_time:
            time_since_frame = time.time() - status.last_frame_time
            if time_since_frame > self.HEALTH_TIMEOUT:
                logger.warning(f"Pipeline {stream_name} has not produced frames for {time_since_frame:.1f}s")
                self._handle_pipeline_failure(stream_name, pipeline, "frame_timeout")
                return

        # Pipeline appears healthy
        self._notify_health_change(stream_name, True, "healthy")

    def _handle_pipeline_failure(self, stream_name: str, pipeline: BasePipeline, reason: str) -> None:
        """Handle pipeline failure with restart logic"""
        with self._lock:
            restart_count = self.restart_counts.get(stream_name, 0)
            last_restart = self.last_restart_times.get(stream_name, 0)

        # Check restart limits
        if restart_count >= self.MAX_RESTART_ATTEMPTS:
            logger.error(f"Pipeline {stream_name} exceeded max restart attempts ({self.MAX_RESTART_ATTEMPTS})")
            self._notify_health_change(stream_name, False, f"max_restarts_exceeded_{reason}")
            return

        # Check backoff timing
        now = time.time()
        backoff_time = self.RESTART_BACKOFF_BASE * (2 ** restart_count)
        time_since_restart = now - last_restart

        if time_since_restart < backoff_time:
            # Still in backoff period
            return

        # Attempt restart
        logger.info(f"Attempting restart {restart_count + 1}/{self.MAX_RESTART_ATTEMPTS} for {stream_name} ({reason})")

        try:
            if pipeline.restart():
                with self._lock:
                    self.restart_counts[stream_name] = restart_count + 1
                    self.last_restart_times[stream_name] = now
                logger.info(f"Successfully restarted pipeline {stream_name}")
                self._notify_health_change(stream_name, True, f"restarted_after_{reason}")
            else:
                logger.error(f"Failed to restart pipeline {stream_name}")
                self._notify_health_change(stream_name, False, f"restart_failed_{reason}")

        except Exception as e:
            logger.exception(f"Exception during pipeline restart {stream_name}: {e}")
            self._notify_health_change(stream_name, False, f"restart_exception_{reason}")

    def _notify_health_change(self, stream_name: str, is_healthy: bool, reason: str) -> None:
        """Notify health change callbacks"""
        for callback in self._health_callbacks:
            try:
                callback(stream_name, is_healthy, reason)
            except Exception as e:
                logger.exception(f"Error in health callback: {e}")

    def get_health_status(self) -> Dict[str, Dict[str, any]]:
        """Get health status of all monitored pipelines"""
        status = {}
        with self._lock:
            for stream_name, pipeline in self.pipelines.items():
                pipeline_status = pipeline.get_status()
                restart_count = self.restart_counts.get(stream_name, 0)
                last_restart = self.last_restart_times.get(stream_name, 0)

                status[stream_name] = {
                    "is_running": pipeline_status.is_running,
                    "is_healthy": pipeline.is_healthy(),
                    "pid": pipeline_status.pid,
                    "restart_count": restart_count,
                    "last_restart": last_restart,
                    "error_message": pipeline_status.error_message,
                }

        return status
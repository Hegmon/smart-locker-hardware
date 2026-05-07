"""
Base Pipeline Module
Abstract base class for all camera streaming pipelines.
"""

from __future__ import annotations
import logging
import subprocess
import signal
import time
import threading
import os
from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    """Configuration for a streaming pipeline"""
    device_path: str
    stream_name: str
    rtsp_host: str = "127.0.0.1"
    rtsp_port: int = 8554
    resolution: str = "640x480"
    framerate: int = 25
    bitrate: str = "1000k"
    codec: str = "libx264"
    preset: str = "ultrafast"


@dataclass
class PipelineStatus:
    """Status of a streaming pipeline"""
    is_running: bool
    pid: Optional[int]
    start_time: Optional[float]
    last_frame_time: Optional[float]
    error_message: Optional[str]
    stats: Dict[str, Any]


class BasePipeline(ABC):
    """Abstract base class for camera streaming pipelines"""

    def __init__(self, config: PipelineConfig):
        self.config = config
        self.process: Optional[subprocess.Popen] = None
        self.status = PipelineStatus(
            is_running=False,
            pid=None,
            start_time=None,
            last_frame_time=None,
            error_message=None,
            stats={}
        )
        self._lock = threading.RLock()

    @abstractmethod
    def get_command(self) -> List[str]:
        """Return the command to run for this pipeline"""
        pass

    @abstractmethod
    def validate_config(self) -> bool:
        """Validate that the pipeline configuration is valid"""
        pass

    def start(self) -> bool:
        """Start the streaming pipeline"""
        with self._lock:
            if self.status.is_running:
                logger.warning(f"Pipeline {self.config.stream_name} already running")
                return True

            if not self.validate_config():
                self.status.error_message = "Invalid configuration"
                return False

            try:
                cmd = self.get_command()
                logger.info(f"Starting pipeline {self.config.stream_name}: {' '.join(cmd)}")

                # Start process
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    preexec_fn=os.setsid if hasattr(os, 'setsid') else None
                )

                self.status.pid = self.process.pid
                self.status.start_time = time.time()
                self.status.last_frame_time = time.time()
                self.status.is_running = True
                self.status.error_message = None

                logger.info(f"Pipeline {self.config.stream_name} started (PID: {self.process.pid})")
                return True

            except Exception as e:
                self.status.error_message = str(e)
                self.status.is_running = False
                logger.exception(f"Failed to start pipeline {self.config.stream_name}: {e}")
                return False

    def stop(self) -> bool:
        """Stop the streaming pipeline"""
        with self._lock:
            if not self.status.is_running or not self.process:
                return True

            try:
                logger.info(f"Stopping pipeline {self.config.stream_name} (PID: {self.process.pid})")

                # Send SIGTERM first
                if hasattr(os, 'killpg'):
                    os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                else:
                    self.process.terminate()

                # Wait for graceful shutdown
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning(f"Pipeline {self.config.stream_name} didn't exit gracefully, sending SIGKILL")
                    if hasattr(os, 'killpg'):
                        os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                    else:
                        self.process.kill()
                    self.process.wait()

                self.status.is_running = False
                self.status.pid = None
                logger.info(f"Pipeline {self.config.stream_name} stopped")
                return True

            except Exception as e:
                logger.exception(f"Error stopping pipeline {self.config.stream_name}: {e}")
                return False

    def is_healthy(self) -> bool:
        """Check if the pipeline is healthy"""
        with self._lock:
            if not self.status.is_running or not self.process:
                return False

            # Check if process is still running
            return self.process.poll() is None

    def get_status(self) -> PipelineStatus:
        """Get current pipeline status"""
        with self._lock:
            # Update running status
            if self.status.is_running and self.process:
                self.status.is_running = self.process.poll() is None
                if not self.status.is_running:
                    self.status.error_message = "Process exited unexpectedly"

            return self.status

    def restart(self) -> bool:
        """Restart the pipeline"""
        logger.info(f"Restarting pipeline {self.config.stream_name}")
        if not self.stop():
            logger.warning(f"Failed to stop pipeline {self.config.stream_name} during restart")
        return self.start()
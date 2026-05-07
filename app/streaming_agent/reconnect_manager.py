"""
Reconnect Manager Module
Handles camera hotplugging and automatic reconnection.
"""

from __future__ import annotations
import logging
import time
import threading
from typing import Dict, Optional, Callable

from .camera_registry import CameraRegistry, CameraDevice
from .pipeline_factory import PipelineFactory
from .pipelines.base_pipeline import BasePipeline, PipelineConfig
from .watchdog import PipelineWatchdog

logger = logging.getLogger(__name__)


class ReconnectManager:
    """Manages automatic reconnection of cameras during hotplug events"""

    def __init__(self,
                 registry: CameraRegistry,
                 watchdog: PipelineWatchdog,
                 rtsp_host: str = "127.0.0.1",
                 rtsp_port: int = 8554):
        self.registry = registry
        self.watchdog = watchdog
        self.rtsp_host = rtsp_host
        self.rtsp_port = rtsp_port

        self.active_pipelines: Dict[str, BasePipeline] = {}
        self.device_to_stream: Dict[str, str] = {}  # device_path -> stream_name

        self._lock = threading.RLock()
        self._callbacks: list[Callable[[str, str, str], None]] = []

        # Register for device change events
        self.registry.add_change_callback(self._on_device_change)

    def add_reconnect_callback(self, callback: Callable[[str, str, str], None]) -> None:
        """
        Add callback for reconnect events.
        Callback signature: (action, device_path, stream_name)
        Actions: "connected", "disconnected", "reconnected"
        """
        with self._lock:
            self._callbacks.append(callback)

    def start_all_cameras(self) -> None:
        """Start streaming for all currently active cameras"""
        devices = self.registry.get_active_devices()

        for device in devices:
            try:
                self._start_device_stream(device)
            except Exception as e:
                logger.exception(f"Failed to start stream for {device.device_path}: {e}")

    def stop_all_streams(self) -> None:
        """Stop all active streams"""
        with self._lock:
            streams_to_stop = list(self.active_pipelines.keys())

        for stream_name in streams_to_stop:
            try:
                self._stop_stream(stream_name)
            except Exception as e:
                logger.exception(f"Failed to stop stream {stream_name}: {e}")

    def _on_device_change(self, action: str, device: CameraDevice, reason: str) -> None:
        """Handle device change events from registry"""
        device_path = device.device_path

        if action == "added":
            logger.info(f"Camera added: {device_path} ({device.classification.backend})")
            try:
                self._start_device_stream(device)
                self._notify_callbacks("connected", device_path, self.device_to_stream.get(device_path, ""))
            except Exception as e:
                logger.exception(f"Failed to start stream for added camera {device_path}: {e}")

        elif action == "removed":
            logger.info(f"Camera removed: {device_path}")
            stream_name = self.device_to_stream.get(device_path)
            if stream_name:
                try:
                    self._stop_stream(stream_name)
                    self._notify_callbacks("disconnected", device_path, stream_name)
                except Exception as e:
                    logger.exception(f"Failed to stop stream for removed camera {device_path}: {e}")

        elif action == "updated":
            logger.info(f"Camera updated: {device_path} ({reason})")
            # For now, just log updates - could implement stream restart if capabilities changed
            pass

    def _start_device_stream(self, device: CameraDevice) -> Optional[str]:
        """Start streaming for a camera device"""
        device_path = device.device_path

        # Generate stream name
        stream_name = self._generate_stream_name(device)

        # Check if already streaming
        if stream_name in self.active_pipelines:
            logger.warning(f"Stream {stream_name} already exists for {device_path}")
            return stream_name

        # Create pipeline
        pipeline = PipelineFactory.create_pipeline(
            device_path=device_path,
            stream_name=stream_name,
            classification=device.classification,
            capabilities=device.capabilities,
            rtsp_host=self.rtsp_host,
            rtsp_port=self.rtsp_port
        )

        if not pipeline:
            logger.error(f"Could not create pipeline for {device_path}")
            return None

        # Start pipeline
        if pipeline.start():
            with self._lock:
                self.active_pipelines[stream_name] = pipeline
                self.device_to_stream[device_path] = stream_name

            # Add to watchdog
            self.watchdog.add_pipeline(stream_name, pipeline)

            logger.info(f"Started stream {stream_name} for camera {device_path}")
            return stream_name
        else:
            logger.error(f"Failed to start pipeline for {device_path}")
            return None

    def _stop_stream(self, stream_name: str) -> bool:
        """Stop a specific stream"""
        with self._lock:
            pipeline = self.active_pipelines.get(stream_name)
            if not pipeline:
                logger.warning(f"Stream {stream_name} not found")
                return False

            # Remove from watchdog first
            self.watchdog.remove_pipeline(stream_name)

            # Stop pipeline
            if pipeline.stop():
                # Clean up mappings
                device_path = pipeline.config.device_path
                self.device_to_stream.pop(device_path, None)
                del self.active_pipelines[stream_name]

                logger.info(f"Stopped stream {stream_name}")
                return True
            else:
                logger.error(f"Failed to stop pipeline {stream_name}")
                return False

    def _generate_stream_name(self, device: CameraDevice) -> str:
        """Generate a unique stream name for a camera"""
        device_path = device.device_path
        backend = device.classification.backend

        # Extract device number
        if "/dev/video" in device_path:
            try:
                dev_num = device_path.replace("/dev/video", "")
                return f"camera_{dev_num}_{backend}"
            except:
                pass

        # Fallback to hash of device path
        import hashlib
        hash_obj = hashlib.md5(device_path.encode())
        return f"camera_{hash_obj.hexdigest()[:8]}_{backend}"

    def _notify_callbacks(self, action: str, device_path: str, stream_name: str) -> None:
        """Notify reconnect event callbacks"""
        for callback in self._callbacks:
            try:
                callback(action, device_path, stream_name)
            except Exception as e:
                logger.exception(f"Error in reconnect callback: {e}")

    def get_active_streams(self) -> Dict[str, str]:
        """Get mapping of stream names to device paths"""
        with self._lock:
            return dict(self.device_to_stream)

    def get_stream_for_device(self, device_path: str) -> Optional[str]:
        """Get stream name for a device path"""
        with self._lock:
            return self.device_to_stream.get(device_path)

    def get_device_for_stream(self, stream_name: str) -> Optional[str]:
        """Get device path for a stream name"""
        with self._lock:
            pipeline = self.active_pipelines.get(stream_name)
            return pipeline.config.device_path if pipeline else None</content>
<parameter name="filePath">/home/hassaanqazi/Documents/smart-locker-hardware/app/streaming_agent/reconnect_manager.py
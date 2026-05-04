"""
Production CCTV Streaming Coordinator

Orchestrates all components for reliable multi-camera streaming.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
import os
from typing import Optional

from .camera_manager import CameraManager
from .stream_worker import StreamWorker
from .stream_watchdog import StreamWatchdog
from .device_safety import DeviceSafetyLayer
from .models import CameraConfig, StreamHealth

# MQTT is optional - only import if available
try:
    from .mqtt_stability import MQTTStabilityLayer, MQTTState
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    MQTTStabilityLayer = None
    MQTTState = None

logger = logging.getLogger(__name__)


class CCTVStreamingCoordinator:
    """
    Production-grade CCTV streaming coordinator.
    
    Manages:
    - Camera discovery and profiling
    - Per-camera stream workers
    - Health monitoring and auto-recovery
    - Device safety
    - MQTT stability (optional)
    
    Features:
    - Self-healing video pipeline
    - Zero manual intervention required
    - Isolated failure domains
    - Comprehensive observability
    """
    
    def __init__(
        self,
        device_id: str,
        mediamtx_host: str = "127.0.0.1",
        mediamtx_port: int = 8554,
        mqtt_host: Optional[str] = None,
        mqtt_port: int = 1883,
        mqtt_username: Optional[str] = None,
        mqtt_password: Optional[str] = None,
        device_uuid: Optional[str] = None,
    ):
        self.device_id = device_id
        self.mediamtx_host = mediamtx_host
        self.mediamtx_port = mediamtx_port
        self.device_uuid = device_uuid
        
        logger.info(
            "Initializing CCTV Streaming Coordinator for device %s",
            device_id,
        )
        
        # Components
        self.camera_manager = CameraManager()
        self.device_safety = DeviceSafetyLayer()
        self.stream_watchdog = StreamWatchdog(on_recovery=self._on_stream_recovered)
        
        # Stream workers (one per camera)
        self.stream_workers: dict[str, StreamWorker] = {}
        
        # MQTT (optional)
        self.mqtt_client: Optional[MQTTStabilityLayer] = None
        if mqtt_host and device_uuid and MQTT_AVAILABLE:
            self.mqtt_client = MQTTStabilityLayer(
                host=mqtt_host,
                port=mqtt_port,
                device_id=device_id,
                device_uuid=device_uuid,
                username=mqtt_username,
                password=mqtt_password,
            )
            self.mqtt_client.set_callbacks(
                on_connect=self._on_mqtt_connect,
                on_disconnect=self._on_mqtt_disconnect,
                on_message=self._on_mqtt_message,
                on_state_change=self._on_mqtt_state_change,
            )
        elif mqtt_host and device_uuid and not MQTT_AVAILABLE:
            logger.warning(
                "MQTT requested but paho-mqtt not installed. "
                "Install with: pip install paho-mqtt"
            )
        
        # State
        self._running = False
        self._initialized = False
        
        # Signal handling
        self._setup_signal_handlers()
        
        logger.info("CCTV Streaming Coordinator initialized")
    
    def _setup_signal_handlers(self) -> None:
        """Set up graceful shutdown on SIGTERM/SIGINT."""
        def signal_handler(signum, frame):
            logger.info("Received signal %d, initiating graceful shutdown", signum)
            self.stop()
            sys.exit(0)
        
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
    
    def initialize(self) -> None:
        """
        Initialize the streaming system.
        
        Discovers cameras, creates stream workers,
        and prepares for streaming.
        """
        logger.info("=== Initializing CCTV Streaming System ===")
        
        # Discover cameras
        logger.info("Discovering cameras...")
        camera_configs = self.camera_manager.discover_cameras()
        
        if not camera_configs:
            logger.warning("No cameras detected!")
        else:
            logger.info("Discovered %d camera(s)", len(camera_configs))
            for config in camera_configs:
                logger.info(
                    "  %s: %s (%s) - %s @ %s",
                    config.camera_type, config.device,
                    config.driver_info, config.format, config.resolution,
                )
        
        # Create stream workers
        for config in camera_configs:
            self._create_stream_worker(config)
        
        # Start MQTT if configured
        if self.mqtt_client:
            logger.info("Connecting to MQTT broker...")
            self.mqtt_client.connect()
        
        self._initialized = True
        logger.info("=== CCTV Streaming System Initialized ===")
    
    def _create_stream_worker(self, config: CameraConfig) -> None:
        """
        Create a stream worker for a camera.
        
        Args:
            config: Camera configuration
        """
        worker = StreamWorker(
            config=config,
            mediamtx_host=self.mediamtx_host,
            mediamtx_port=self.mediamtx_port,
            device_id=self.device_id,
            on_health_change=self._on_worker_health_change,
        )
        
        self.stream_workers[config.camera_type] = worker
        self.stream_watchdog.add_worker(worker)
        
        logger.info(
            "Created stream worker for %s (%s)",
            config.camera_type, config.device,
        )
    
    def start(self) -> None:
        """Start streaming for all cameras."""
        if not self._initialized:
            logger.warning("System not initialized, initializing now...")
            self.initialize()
        
        logger.info("=== Starting CCTV Streaming System ===")
        self._running = True
        
        # Start stream workers
        for camera_type, worker in self.stream_workers.items():
            if worker.start():
                logger.info("Started stream for %s", camera_type)
            else:
                logger.error("Failed to start stream for %s", camera_type)
        
        # Start watchdog
        self.stream_watchdog.start()
        
        logger.info("=== CCTV Streaming System Started ===")
        
        # Publish system status
        self._publish_system_status()
    
    def stop(self) -> None:
        """Stop all streaming and clean up."""
        if not self._running:
            return
        
        logger.info("=== Stopping CCTV Streaming System ===")
        self._running = False
        
        # Stop watchdog
        self.stream_watchdog.stop()
        
        # Stop stream workers
        for camera_type, worker in self.stream_workers.items():
            logger.info("Stopping stream for %s", camera_type)
            worker.stop()
        
        # Disconnect MQTT
        if self.mqtt_client:
            self.mqtt_client.disconnect()
        
        # Clean up device safety layer
        self.device_safety.cleanup()
        
        logger.info("=== CCTV Streaming System Stopped ===")
    
    def get_status(self) -> dict:
        """
        Get system status.
        
        Returns:
            Dictionary with system status
        """
        return {
            "running": self._running,
            "initialized": self._initialized,
            "device_id": self.device_id,
            "mediamtx": {
                "host": self.mediamtx_host,
                "port": self.mediamtx_port,
            },
            "mqtt": (
                self.mqtt_client.get_stats()
                if self.mqtt_client else {"enabled": False}
            ),
            "cameras": {
                camera_type: worker.get_health.to_dict()
                for camera_type, worker in self.stream_workers.items()
            },
            "watchdog": self.stream_watchdog.get_status(),
            "active_devices": self.device_safety.get_active_devices(),
        }
    
    def _on_worker_health_change(self, health: StreamHealth) -> None:
        """
        Handle stream worker health change.
        
        Args:
            health: Updated health status
        """
        logger.info(
            "Stream health update: %s - %s (uptime: %.0fs, fps: %.1f)",
            health.camera_type, health.state,
            health.uptime_seconds, health.fps,
        )
        
        # Publish to MQTT if connected
        if self.mqtt_client and self.mqtt_client.is_connected():
            topic = f"devices/{self.device_uuid}/events/stream"
            payload = {
                "device_id": self.device_id,
                "camera_type": health.camera_type,
                "state": health.state,
                "format": health.format,
                "resolution": health.resolution,
                "uptime_seconds": health.uptime_seconds,
                "fps": health.fps,
                "restart_count": health.restart_count,
            }
            self.mqtt_client.publish(topic, payload, qos=1)
    
    def _on_stream_recovered(self, camera_type: str) -> None:
        """
        Handle stream recovery.
        
        Args:
            camera_type: Recovered camera type
        """
        logger.info("Stream recovered: %s", camera_type)
        
        if self.mqtt_client and self.mqtt_client.is_connected():
            topic = f"devices/{self.device_uuid}/events/recovery"
            payload = {
                "device_id": self.device_id,
                "camera_type": camera_type,
                "event": "stream_recovered",
            }
            self.mqtt_client.publish(topic, payload, qos=1)
    
    def _on_mqtt_connect(self) -> None:
        """Handle MQTT connection."""
        logger.info("MQTT connected")
        
        # Subscribe to command topics
        if self.device_uuid:
            topic = f"devices/{self.device_uuid}/commands/stream"
            self.mqtt_client.subscribe(topic, qos=1)
            logger.info("Subscribed to %s", topic)
        
        # Publish connection status
        self._publish_system_status()
    
    def _on_mqtt_disconnect(self) -> None:
        """Handle MQTT disconnection."""
        logger.warning("MQTT disconnected")
    
    def _on_mqtt_message(self, topic: str, payload: dict) -> None:
        """
        Handle MQTT message.
        
        Args:
            topic: Message topic
            payload: Message payload
        """
        logger.info("MQTT message on %s: %s", topic, payload)
        
        # Handle stream commands
        if "commands/stream" in topic:
            self._handle_stream_command(payload)
    
    def _on_mqtt_state_change(self, old_state: MQTTState, new_state: MQTTState) -> None:
        """
        Handle MQTT state change.
        
        Args:
            old_state: Previous state
            new_state: New state
        """
        logger.info("MQTT state: %s → %s", old_state.value, new_state.value)
    
    def _handle_stream_command(self, payload: dict) -> None:
        """
        Handle stream control command.
        
        Args:
            payload: Command payload
        """
        action = payload.get("action")
        camera_type = payload.get("camera_type", "all")
        
        logger.info("Stream command: %s for %s", action, camera_type)
        
        if action == "start":
            if camera_type == "all":
                for worker in self.stream_workers.values():
                    worker.start()
            elif camera_type in self.stream_workers:
                self.stream_workers[camera_type].start()
        
        elif action == "stop":
            if camera_type == "all":
                for worker in self.stream_workers.values():
                    worker.stop()
            elif camera_type in self.stream_workers:
                self.stream_workers[camera_type].stop()
        
        elif action == "restart":
            if camera_type == "all":
                for worker in self.stream_workers.values():
                    worker.stop()
                    time.sleep(1)
                    worker.start()
            elif camera_type in self.stream_workers:
                worker = self.stream_workers[camera_type]
                worker.stop()
                time.sleep(1)
                worker.start()
        
        elif action == "status":
            # Status is automatically published via health updates
            pass
    
    def _publish_system_status(self) -> None:
        """Publish system status to MQTT."""
        if not (self.mqtt_client and self.mqtt_client.is_connected()):
            return
        
        topic = f"devices/{self.device_uuid}/status/system"
        payload = self.get_status()
        self.mqtt_client.publish(topic, payload, qos=1, retain=True)
        logger.debug("Published system status")
    
    def run(self) -> None:
        """Run the streaming system."""
        logger.info("=== CCTV Streaming System Running ===")
        
        self.initialize()
        self.start()
        
        # Main loop
        try:
            while self._running:
                time.sleep(1)
                
                # Periodic status update (every 60 seconds)
                if hasattr(self, '_last_status_update'):
                    if time.time() - self._last_status_update > 60:
                        self._publish_system_status()
                        self._last_status_update = time.time()
                else:
                    self._last_status_update = time.time()
        
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt")
        
        finally:
            self.stop()
    
    def add_camera(self, device_path: str) -> bool:
        """
        Dynamically add a camera.
        
        Args:
            device_path: Path to video device
        
        Returns:
            True if camera added successfully
        """
        logger.info("Adding camera: %s", device_path)
        
        config = self.camera_manager.get_camera_config(device_path)
        if not config:
            logger.error("Failed to probe camera: %s", device_path)
            return False
        
        if config.camera_type in self.stream_workers:
            logger.warning(
                "Camera %s already exists, replacing",
                config.camera_type,
            )
            self.stream_workers[config.camera_type].stop()
        
        self._create_stream_worker(config)
        
        if self._running:
            self.stream_workers[config.camera_type].start()
        
        logger.info("Camera added: %s", config.camera_type)
        return True
    
    def remove_camera(self, camera_type: str) -> bool:
        """
        Remove a camera.
        
        Args:
            camera_type: Type of camera to remove
        
        Returns:
            True if camera removed
        """
        if camera_type not in self.stream_workers:
            logger.warning("Camera not found: %s", camera_type)
            return False
        
        logger.info("Removing camera: %s", camera_type)
        
        worker = self.stream_workers[camera_type]
        worker.stop()
        self.stream_watchdog.remove_worker(camera_type)
        del self.stream_workers[camera_type]
        
        logger.info("Camera removed: %s", camera_type)
        return True


def main():
    """Main entry point."""
    import os
    
    # Check if streaming v2 is enabled
    use_v2 = os.getenv("USE_STREAMING_V2", "false").lower()
    use_v2 = use_v2 in ("1", "true", "yes", "on")
    
    if not use_v2:
        logger.info("Streaming v2 is disabled (USE_STREAMING_V2 not set)")
        logger.info("Set USE_STREAMING_V2=true to enable")
        return
    
    # Get configuration from environment
    device_id = os.getenv("DEVICE_ID", "test-device")
    device_uuid = os.getenv("DEVICE_UUID", "")
    mediamtx_host = os.getenv("MEDIAMTX_HOST", "127.0.0.1")
    mediamtx_port = int(os.getenv("MEDIAMTX_RTSP_PORT", "8554"))
    mqtt_host = os.getenv("MQTT_HOST")
    mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
    mqtt_username = os.getenv("MQTT_USERNAME")
    mqtt_password = os.getenv("MQTT_PASSWORD")
    
    # Create coordinator
    coordinator = CCTVStreamingCoordinator(
        device_id=device_id,
        mediamtx_host=mediamtx_host,
        mediamtx_port=mediamtx_port,
        mqtt_host=mqtt_host,
        mqtt_port=mqtt_port,
        mqtt_username=mqtt_username,
        mqtt_password=mqtt_password,
        device_uuid=device_uuid or None,
    )
    
    # Run
    coordinator.run()


if __name__ == "__main__":
    main()


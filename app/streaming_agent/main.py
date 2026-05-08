"""
Streaming Agent for Raspberry Pi 4
Main entry point for the camera streaming system.

Architecture:
- Camera Detection: auto-detect internal vs external cameras
- FFmpeg Stream Supervisor: resilient stream engine with health monitoring
- MQTT Handler: respond to stream control commands
- Stream Verifier: validate RTSP + HLS output

Run as systemd service: qbox-streaming.service
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from threading import Event
from typing import Optional

from app.deployment.bootstrap import bootstrap_device
from app.deployment.health_server import AgentHealthServer
from app.deployment.runtime_config import get_bool_setting, get_int_setting, get_str_setting
from app.deployment.validation import validate_runtime_configuration

# Configure logging early
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("streaming_agent")

# Import components
from .camera_detector import CameraDetector
from .camera_registry import CameraRegistry
from .camera_classifier import CameraClassifier
from .camera_capabilities import CameraCapabilitiesDetector
from .pipeline_factory import PipelineFactory
from .watchdog import PipelineWatchdog
from .reconnect_manager import ReconnectManager
from .health_monitor import HealthMonitor
from .constants import CAMERA_EXTERNAL, CAMERA_INTERNAL, MEDIAMTX_HOST, MEDIAMTX_RTSP_PORT
from .device_config import get_device_config
from .ffmpeg_manager import FFmpegStreamEngine, ProductionCameraPipeline
from .mqtt_handler import StreamingMQTTClient
from .stream_verifier import StreamVerifier
from .urls import build_hls_url, build_rtsp_url


class StreamingAgent:
    """Main streaming agent orchestrator"""
    
    def __init__(self):
        self._stop_event = Event()
        bootstrap_device()
        self._device_config = get_device_config()
        self.device_id = self._device_config["device_id"]
        self.device_uuid = self._device_config.get("device_uuid", "")
        self.mediamtx_host = self._device_config.get("mediamtx_host") or MEDIAMTX_HOST
        self.mediamtx_rtsp_port = self._safe_int(
            self._device_config.get("mediamtx_rtsp_port"),
            MEDIAMTX_RTSP_PORT,
        )
        
        logger.info("Initializing Streaming Agent")
        logger.info("  device_id: %s", self.device_id)
        logger.info("  device_uuid: %s", self.device_uuid or "(not set)")
        logger.info("  mediamtx_host: %s", self.mediamtx_host)
        logger.info("  mediamtx_rtsp_port: %s", self.mediamtx_rtsp_port)
        
        # New modular components for robust camera management
        self.camera_registry = CameraRegistry()
        self.classifier = CameraClassifier()
        self.capabilities_detector = CameraCapabilitiesDetector()
        self.pipeline_factory = PipelineFactory()
        self.watchdog = PipelineWatchdog()
        self.reconnect_manager = ReconnectManager(
            self.camera_registry,
            self.watchdog,
            self.mediamtx_host,
            self.mediamtx_rtsp_port
        )
        self.health_monitor = HealthMonitor(
            self.camera_registry,
            self.watchdog,
            self.reconnect_manager
        )

        # Legacy components (kept for backward compatibility)
        self.detector = CameraDetector()
        self.ffmpeg_manager: Optional[FFmpegStreamEngine] = None
        self.pipeline: Optional[ProductionCameraPipeline] = None
        self.mqtt_client: Optional[StreamingMQTTClient] = None
        self.verifier: Optional[StreamVerifier] = None
        self.health_server = AgentHealthServer(
            "0.0.0.0",
            get_int_setting("STREAMING_AGENT_HEALTH_PORT", 8092),
            self._health_payload,
        )
        
        # Auto-start on boot flag (controlled via env or config)
        self.auto_start = get_bool_setting("STREAM_AUTO_START", True)
    
    @staticmethod
    def _read_bool_env(name: str, default: bool = False) -> bool:
        import os
        val = os.getenv(name)
        if val is None:
            return default
        return val.strip().lower() in {"1", "true", "yes", "on"}
    
    @staticmethod
    def _safe_int(value: object, default: int) -> int:
        try:
            if value in (None, ""):
                return default
            return int(value)
        except (TypeError, ValueError):
            return default
    
    def initialize(self) -> None:
        """Initialize all components"""
        logger.info("=== Initializing Streaming Agent ===")

        # Initialize new modular camera management system
        logger.info("Starting camera registry monitoring...")
        self.camera_registry.start_monitoring()

        # Perform initial comprehensive camera scan
        devices = self.camera_registry.scan_devices()
        logger.info("Initial camera registry scan: %d devices found", len(devices))

        for device_path, device in devices.items():
            logger.info("  %s: backend=%s, type=%s, capabilities=%s",
                       device_path,
                       device.classification.backend,
                       device.classification.device_type,
                       device.capabilities.capabilities)

        # Setup reconnect manager callbacks for dynamic camera handling
        self.reconnect_manager.add_reconnect_callback(self._on_camera_reconnect)

        # Start watchdog for pipeline health monitoring
        self.watchdog.start()

        # Legacy camera detection for backward compatibility with existing pipeline
        cameras = self._detect_cameras_with_retry()
        logger.info("Legacy camera detection: %s", list(cameras.keys()))
        for cam_type, cam_info in cameras.items():
            logger.info("  %s: %s (backend=%s)", cam_type, cam_info.device_path,
                       getattr(cam_info, 'backend', 'unknown'))

        if not cameras:
            logger.warning("No cameras detected via legacy method!")

        # Initialize Production Camera Pipeline (multi-camera aware)
        self.pipeline = ProductionCameraPipeline(
            device_id=self.device_id,
            mediamtx_host=self.mediamtx_host,
            mediamtx_rtsp_port=self.mediamtx_rtsp_port,
        )

        # Use camera registry to configure pipelines for all valid devices
        try:
            registry_devices = self.camera_registry.get_active_devices()
            if registry_devices:
                logger.info("Configuring pipeline from registry for %d device(s)", len(registry_devices))
                # Use new multi-device setup method which preserves internal/external naming
                self.pipeline.setup_from_registry(list(registry_devices))
            else:
                # Fallback to legacy behavior using detector results
                internal_device = None
                external_device = None
                internal_backend = "v4l2"
                external_backend = "v4l2"

                if "internal" in cameras:
                    internal_device = cameras["internal"].device_path
                    internal_backend = getattr(cameras["internal"], 'backend', 'v4l2')
                if "external" in cameras:
                    external_device = cameras["external"].device_path
                    external_backend = getattr(cameras["external"], 'backend', 'v4l2')

                # Fallback manual detection with backend classification
                if not internal_device and not external_device:
                    logger.info("No cameras from legacy detector, attempting manual detection...")
                    import glob
                    video_devices = sorted(glob.glob("/dev/video*"))
                    if len(video_devices) >= 1:
                        internal_device = video_devices[0]
                        internal_name = self.detector._get_device_name(internal_device)
                        internal_backend = self.detector._classify_camera_backend(internal_device, internal_name)
                    if len(video_devices) >= 2:
                        external_device = video_devices[1]
                        external_name = self.detector._get_device_name(external_device)
                        external_backend = self.detector._classify_camera_backend(external_device, external_name)

                if internal_device:
                    logger.info("Setting up internal camera: %s (backend=%s)", internal_device, internal_backend)
                if external_device:
                    logger.info("Setting up external camera: %s (backend=%s)", external_device, external_backend)

                self._setup_pipeline_with_retry(internal_device, external_device, internal_backend, external_backend)

            # Add virtual pipelines to watchdog for monitoring
            internal_pipeline = self.pipeline.get_virtual_pipeline("internal")
            if internal_pipeline:
                self.watchdog.add_pipeline("internal", internal_pipeline)
                logger.info("Added internal camera to watchdog monitoring")

            external_pipeline = self.pipeline.get_virtual_pipeline("external")
            if external_pipeline:
                self.watchdog.add_pipeline("external", external_pipeline)
                logger.info("Added external camera to watchdog monitoring")

        except Exception as e:
            logger.exception("Failed to configure production pipeline from registry: %s", e)
        
        # 3. Initialize verifier
        self.verifier = StreamVerifier(
            device_id=self.device_id,
            mediamtx_host=self.mediamtx_host,
            rtsp_port=self.mediamtx_rtsp_port,
        )
        
        # 4. Initialize MQTT client if device_uuid available
        if self.device_uuid:
            self._init_mqtt()
        else:
            logger.warning("device_uuid not set - MQTT command handling disabled")
            logger.warning("Register the device with Django backend first")
        
        logger.info("=== Initialization Complete ===")

    def _detect_cameras_with_retry(self, attempts: int = 3) -> dict:
        logger.info("--- Camera Detection Phase ---")
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self.detector.get_cameras_for_streaming()
            except Exception as exc:
                last_error = exc
                logger.warning("Camera detection attempt %s/%s failed: %s", attempt, attempts, exc)
                time.sleep(attempt)
        logger.warning("Camera detection unavailable after retries: %s", last_error)
        return {}

    def _setup_pipeline_with_retry(
        self,
        internal_device: str | None,
        external_device: str | None,
        internal_backend: str = "v4l2",
        external_backend: str = "v4l2",
        attempts: int = 3,
    ) -> None:
        for attempt in range(1, attempts + 1):
            try:
                self.pipeline.setup_cameras(
                    internal_device=internal_device,
                    external_device=external_device,
                    internal_backend=internal_backend,
                    external_backend=external_backend,
                )
                return
            except Exception as exc:
                logger.warning("Pipeline setup attempt %s/%s failed: %s", attempt, attempts, exc)
                time.sleep(attempt)
        logger.error("Failed to setup pipeline after retries; continuing without blocking boot")
    
    def _init_mqtt(self) -> None:
        """Initialize MQTT client and register command handler"""
        mqtt_host = get_str_setting("MQTT_HOST", "69.62.125.223")
        mqtt_port = get_int_setting("MQTT_PORT", 1883)
        mqtt_username = get_str_setting("MQTT_USERNAME", "qbox")
        mqtt_password = get_str_setting("MQTT_PASSWORD", "strongpassword123")
        
        self.mqtt_client = StreamingMQTTClient(
            host=mqtt_host,
            port=mqtt_port,
            device_uuid=self.device_uuid,
            device_id=self.device_id,
            username=mqtt_username,
            password=mqtt_password,
            command_handler=self._handle_mqtt_command,
        )
        
        self.mqtt_client.connect()
        
        # Wait for connection (with timeout)
        timeout = 5.0
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if self.mqtt_client.is_connected():
                logger.info("MQTT connected")
                return
            time.sleep(0.1)
        
        logger.warning("MQTT connection timeout after %.1fs", timeout)
    
    def _handle_mqtt_command(self, payload: dict, topic: str) -> dict:
        """
        Handle incoming MQTT command.
        Topic pattern: devices/{uuid}/services/stream/request
        """
        command_id = payload.get("command_id", "")
        service = payload.get("service", "stream")
        data = payload.get("data", {})
        
        logger.info("MQTT command: service=%s, data=%s", service, data)
        
        # Handle stream.start/stop/status/restart
        if service == "stream":
            action = data.get("action", "status")
            stream_type = data.get("stream_type", "all")
            
            if action == "start":
                return self._cmd_start(stream_type)
            elif action == "stop":
                return self._cmd_stop(stream_type)
            elif action == "restart":
                return self._cmd_restart(stream_type)
            elif action == "status":
                return self._cmd_status(stream_type)
            else:
                return {
                    "status": "ERROR",
                    "message": f"Unknown action: {action}",
                }
        
        return {
            "status": "UNSUPPORTED",
            "message": f"Service {service} not handled by streaming agent",
        }
    
    def _cmd_start(self, stream_type: str) -> dict:
        """Start stream(s)"""
        if not self.pipeline:
            return {"status": "ERROR", "message": "Pipeline not initialized"}
        
        if stream_type == "all":
            self.pipeline.start()
            return {"status": "SUCCESS", "started": "all"}
        else:
            # Start specific stream via pipeline
            # For backward compatibility, use ffmpeg_manager if available
            if self.ffmpeg_manager:
                ok = self.ffmpeg_manager.start_stream(stream_type)
                return {
                    "status": "SUCCESS" if ok else "ERROR",
                    "stream_type": stream_type,
                    "started": ok,
                    **self._stream_urls(stream_type),
                }
            return {"status": "ERROR", "message": "No stream manager available"}
    
    def _cmd_stop(self, stream_type: str) -> dict:
        """Stop stream(s)"""
        if stream_type == "all":
            if self.ffmpeg_manager:
                self.ffmpeg_manager.stop_all()
            return {"status": "SUCCESS", "stopped_all": True}
        else:
            if self.ffmpeg_manager:
                ok = self.ffmpeg_manager.stop_stream(stream_type)
                return {
                    "status": "SUCCESS" if ok else "ERROR",
                    "stream_type": stream_type,
                    "stopped": ok,
                }
            return {"status": "ERROR", "message": "No stream manager available"}
    
    def _cmd_restart(self, stream_type: str) -> dict:
        """Restart stream(s)"""
        if stream_type == "all":
            results = {}
            for st in [CAMERA_INTERNAL, CAMERA_EXTERNAL]:
                if self.ffmpeg_manager:
                    self.ffmpeg_manager.stop_stream(st)
                    ok = self.ffmpeg_manager.start_stream(st)
                    results[st] = {"restarted": ok, **self._stream_urls(st)}
            return {"status": "SUCCESS", "streams": results}
        else:
            if self.ffmpeg_manager:
                self.ffmpeg_manager.stop_stream(stream_type)
                ok = self.ffmpeg_manager.start_stream(stream_type)
                return {
                    "status": "SUCCESS" if ok else "ERROR",
                    "stream_type": stream_type,
                    "restarted": ok,
                    **self._stream_urls(stream_type),
                }
            return {"status": "ERROR", "message": "No stream manager available"}
    
    def _cmd_status(self, stream_type: str) -> dict:
        """Get stream status"""
        if self.pipeline:
            pipeline_status = self.pipeline.get_pipeline_status()
            return {
                "status": "SUCCESS",
                "pipeline": pipeline_status,
            }
        
        if self.ffmpeg_manager:
            if stream_type == "all":
                streams = self.ffmpeg_manager.get_all_status()
                for st, status in streams.items():
                    status.update(self._stream_urls(st))
                return {
                    "status": "SUCCESS",
                    "streams": streams,
                }
            else:
                s = self.ffmpeg_manager.get_stream_status(stream_type)
                if s:
                    s.update(self._stream_urls(stream_type))
                    return {"status": "SUCCESS", "stream": s}
                else:
                    return {"status": "ERROR", "message": f"Unknown stream: {stream_type}"}
        
        return {"status": "ERROR", "message": "No stream manager available"}

    def _on_camera_reconnect(self, action: str, device_path: str, stream_name: str) -> None:
        """Handle camera reconnect events"""
        logger.info("Camera %s event: %s -> %s", action, device_path, stream_name)

        # Publish MQTT status update if MQTT is available
        if self.mqtt_client and self.device_uuid:
            self.mqtt_client.publish_status_event({
                "type": "camera_event",
                "action": action,
                "device_path": device_path,
                "stream_name": stream_name,
                "timestamp": time.time()
            })

    def _stream_urls(self, stream_type: str) -> dict:
        """Build stream URLs for responses and status events."""
        return {
            "hls_url": build_hls_url(stream_type, device_id=self.device_id),
            "rtsp_url": build_rtsp_url(stream_type, device_id=self.device_id),
        }
    
    def _on_stream_status_change(self, camera_type: str, new_status: str, details: str = "") -> None:
        """Callback when a stream's status changes"""
        logger.info("Stream %s status changed: %s (%s)", camera_type, new_status, details)
        if self.mqtt_client:
            self.mqtt_client.publish_status_event({
                "type": "stream_status",
                "camera_type": camera_type,
                "status": new_status,
                "details": details,
                **self._stream_urls(camera_type),
            })
    
    def run(self) -> None:
        """Main agent run loop"""
        logger.info("=== Starting Streaming Agent ===")
        
        validate_runtime_configuration()
        self.health_server.start()
        self.initialize()
        
        # Auto-start streams if configured
        if self.auto_start:
            logger.info("Auto-start enabled, starting streams...")
            # Start new modular streaming system
            self.reconnect_manager.start_all_cameras()
            # Also start legacy pipeline for backward compatibility
            if self.pipeline:
                self.pipeline.start()
            elif self.ffmpeg_manager:
                cameras = self.ffmpeg_manager._streams
                if cameras:
                    logger.info("Auto-starting %d legacy camera streams...", len(cameras))
                    self.ffmpeg_manager.start_all()
                else:
                    logger.warning("No cameras to auto-start")
            else:
                logger.warning("No stream manager available for auto-start")
        else:
            logger.info("Auto-start disabled (STREAM_AUTO_START=false)")
        
        # Wait for stop signal
        try:
            while not self._stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Received interrupt")
        
        self.shutdown()
    
    def shutdown(self) -> None:
        """Clean shutdown"""
        logger.info("=== Shutting down Streaming Agent ===")

        # Stop new modular components
        self.reconnect_manager.stop_all_streams()
        self.watchdog.stop()
        self.camera_registry.stop_monitoring()

        # Stop legacy components
        if self.pipeline:
            self.pipeline.stop()

        if self.ffmpeg_manager:
            self.ffmpeg_manager.stop_monitoring()
            self.ffmpeg_manager.stop_all()

        if self.mqtt_client:
            self.mqtt_client.disconnect()

        self.health_server.stop()

        logger.info("=== Shutdown complete ===")

    def _health_payload(self) -> dict:
        pipeline_status = {}
        if self.pipeline:
            try:
                pipeline_status = self.pipeline.get_pipeline_status()
            except Exception:
                pipeline_status = {"status": "unknown"}

        # Get comprehensive health from new health monitor
        try:
            comprehensive_health = self.health_monitor.get_system_health()
        except Exception:
            comprehensive_health = {"status": "error", "message": "Health monitor unavailable"}

        return {
            "status": "ok",
            "service": "streaming_agent",
            "device_id": self.device_id,
            "mqtt_connected": self.mqtt_client.is_connected() if self.mqtt_client else False,
            "auto_start": self.auto_start,
            "pipeline": pipeline_status,
            "comprehensive_health": comprehensive_health,
        }


def main() -> int:
    """Entry point for streaming agent"""
    import os

    # If the new streaming_v2 coordinator is requested, delegate to it.
    use_v2 = os.getenv("USE_STREAMING_V2", "false").strip().lower() in ("1", "true", "yes", "on")
    if use_v2:
        try:
            from app.streaming_v2.coordinator import CCTVStreamingCoordinator

            device_cfg = get_device_config()
            device_id = device_cfg.get("device_id", "unknown")
            device_uuid = device_cfg.get("device_uuid", "") or None
            mediamtx_host = device_cfg.get("mediamtx_host") or MEDIAMTX_HOST
            mediamtx_port = int(device_cfg.get("mediamtx_rtsp_port") or MEDIAMTX_RTSP_PORT)

            mqtt_host = get_str_setting("MQTT_HOST", None)
            mqtt_port = get_int_setting("MQTT_PORT", 1883)
            mqtt_username = get_str_setting("MQTT_USERNAME", None)
            mqtt_password = get_str_setting("MQTT_PASSWORD", None)

            coordinator = CCTVStreamingCoordinator(
                device_id=device_id,
                mediamtx_host=mediamtx_host,
                mediamtx_port=mediamtx_port,
                mqtt_host=mqtt_host,
                mqtt_port=mqtt_port,
                mqtt_username=mqtt_username,
                mqtt_password=mqtt_password,
                device_uuid=device_uuid,
            )

            coordinator.run()
            return 0
        except Exception:
            logger.exception("Failed to start streaming_v2 coordinator")
            return 1

    agent = StreamingAgent()
    
    # Set up signal handlers
    def signal_handler(signum, frame):
        logger.info("Received signal %d", signum)
        agent._stop_event.set()
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        agent.run()
        return 0
    except Exception:
        logger.exception("Fatal error in streaming agent")
        return 1


if __name__ == "__main__":
    sys.exit(main())

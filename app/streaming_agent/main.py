"""
Streaming Agent for Raspberry Pi 4
Main entry point for the camera streaming system.

Architecture:
- Camera Detection: auto-detect internal vs external cameras
- FFmpeg Manager: manage subprocesses, auto-restart on failure
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

# Configure logging early
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("streaming_agent")

# Import components
from .camera_detector import CameraDetector
from .constants import CAMERA_EXTERNAL, CAMERA_INTERNAL
from .device_config import get_device_config
from .ffmpeg_manager import FFmpegManager
from .mqtt_handler import StreamingMQTTClient
from .stream_verifier import StreamVerifier


class StreamingAgent:
    """Main streaming agent orchestrator"""
    
    def __init__(self):
        self._stop_event = Event()
        self._device_config = get_device_config()
        self.device_id = self._device_config["device_id"]
        self.device_uuid = self._device_config.get("device_uuid", "")
        
        logger.info("Initializing Streaming Agent")
        logger.info("  device_id: %s", self.device_id)
        logger.info("  device_uuid: %s", self.device_uuid or "(not set)")
        
        # Components
        self.detector = CameraDetector()
        self.ffmpeg_manager: Optional[FFmpegManager] = None
        self.mqtt_client: Optional[StreamingMQTTClient] = None
        self.verifier: Optional[StreamVerifier] = None
        
        # Auto-start on boot flag (controlled via env or config)
        self.auto_start = self._read_bool_env("STREAM_AUTO_START", True)
    
    @staticmethod
    @staticmethod
    def _read_bool_env(name: str, default: bool = False) -> bool:
        import os
        val = os.getenv(name)
        if val is None:
            return default
        return val.strip().lower() in {"1", "true", "yes", "on"}
    
    def initialize(self) -> None:
        """Initialize all components"""
        logger.info("=== Initializing Streaming Agent ===")
        
        # 1. Detect cameras
        cameras = self.detector.get_cameras_for_streaming()
        logger.info("Detected cameras: %s", list(cameras.keys()))
        for cam_type, cam_info in cameras.items():
            logger.info("  %s: %s", cam_type, cam_info.device_path)
        
        if not cameras:
            logger.warning("No cameras detected! Streaming will not start.")
        
        # 2. Initialize FFmpeg manager
        self.ffmpeg_manager = FFmpegManager(
            device_id=self.device_id,
            on_stream_status_change=self._on_stream_status_change
        )
        
        # Register cameras
        for cam_type, cam_info in cameras.items():
            self.ffmpeg_manager.add_stream(cam_type, cam_info.device_path)
        
        # 3. Initialize stream verifier
        self.verifier = StreamVerifier(device_id=self.device_id)
        
        # 4. Initialize MQTT client if device_uuid available
        if self.device_uuid:
            self._init_mqtt()
        else:
            logger.warning("device_uuid not set - MQTT command handling disabled")
            logger.warning("Register the device with Django backend first")
        
        logger.info("=== Initialization Complete ===")
    
    def _init_mqtt(self) -> None:
        """Initialize MQTT client and register command handler"""
        import os
        
        mqtt_host = os.getenv("MQTT_HOST", "69.62.125.223")
        mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
        mqtt_username = os.getenv("MQTT_USERNAME", "qbox")
        mqtt_password = os.getenv("MQTT_PASSWORD", "strongpassword123")
        
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
        if stream_type == "all":
            results = {}
            all_ok = True
            for st in [CAMERA_INTERNAL, CAMERA_EXTERNAL]:
                ok = self.ffmpeg_manager.start_stream(st)
                results[st] = {"started": ok}
                if not ok:
                    all_ok = False
            status = "SUCCESS" if all_ok else "PARTIAL"
            return {"status": status, "streams": results}
        else:
            ok = self.ffmpeg_manager.start_stream(stream_type)
            return {
                "status": "SUCCESS" if ok else "ERROR",
                "stream_type": stream_type,
                "started": ok,
            }
    
    def _cmd_stop(self, stream_type: str) -> dict:
        """Stop stream(s)"""
        if stream_type == "all":
            self.ffmpeg_manager.stop_all()
            return {"status": "SUCCESS", "stopped_all": True}
        else:
            ok = self.ffmpeg_manager.stop_stream(stream_type)
            return {
                "status": "SUCCESS" if ok else "ERROR",
                "stream_type": stream_type,
                "stopped": ok,
            }
    
    def _cmd_restart(self, stream_type: str) -> dict:
        """Restart stream(s)"""
        if stream_type == "all":
            results = {}
            for st in [CAMERA_INTERNAL, CAMERA_EXTERNAL]:
                self.ffmpeg_manager.stop_stream(st)
                ok = self.ffmpeg_manager.start_stream(st)
                results[st] = {"restarted": ok}
            return {"status": "SUCCESS", "streams": results}
        else:
            self.ffmpeg_manager.stop_stream(stream_type)
            ok = self.ffmpeg_manager.start_stream(stream_type)
            return {
                "status": "SUCCESS" if ok else "ERROR",
                "stream_type": stream_type,
                "restarted": ok,
            }
    
    def _cmd_status(self, stream_type: str) -> dict:
        """Get stream status"""
        if stream_type == "all":
            return {
                "status": "SUCCESS",
                "streams": self.ffmpeg_manager.get_all_status(),
            }
        else:
            s = self.ffmpeg_manager.get_stream_status(stream_type)
            if s:
                return {"status": "SUCCESS", "stream": s}
            else:
                return {"status": "ERROR", "message": f"Unknown stream: {stream_type}"}
    
    def _on_stream_status_change(self, camera_type: str, new_status: str) -> None:
        """Callback when a stream's status changes"""
        logger.info("Stream %s status changed: %s", camera_type, new_status)
        if self.mqtt_client:
            self.mqtt_client.publish_status_event({
                "type": "stream_status",
                "camera_type": camera_type,
                "status": new_status,
            })
    
    def run(self) -> None:
        """Main agent run loop"""
        logger.info("=== Starting Streaming Agent ===")
        
        self.initialize()
        
        # Auto-start streams if configured
        if self.auto_start:
            cameras = self.ffmpeg_manager._streams
            if cameras:
                logger.info("Auto-starting %d camera streams...", len(cameras))
                self.ffmpeg_manager.start_all()
            else:
                logger.warning("No cameras to auto-start")
        else:
            logger.info("Auto-start disabled (STREAM_AUTO_START=false)")
        
        # Start monitoring
        if self.ffmpeg_manager:
            self.ffmpeg_manager.start_monitoring()
        
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
        
        if self.ffmpeg_manager:
            self.ffmpeg_manager.stop_monitoring()
            self.ffmpeg_manager.stop_all()
        
        if self.mqtt_client:
            self.mqtt_client.disconnect()
        
        logger.info("=== Shutdown complete ===")


def main() -> int:
    """Entry point for streaming agent"""
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

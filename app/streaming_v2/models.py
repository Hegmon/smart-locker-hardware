"""
Camera Configuration and Data Models
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
import json


@dataclass
class CameraConfig:
    """
    Per-camera configuration discovered during probing.
    
    Attributes:
        device: Device path (e.g., /dev/video0)
        format: Selected pixel format (mjpeg, yuyv422, h264)
        resolution: Video resolution (e.g., 640x480, 1280x720)
        supported_formats: All formats detected on device
        safe_resolutions: Tested working resolutions
        driver_info: V4L2 driver name
        camera_type: internal or external
        physical_id: Stable hardware identifier
    """
    device: str
    format: str
    resolution: str
    supported_formats: List[str] = field(default_factory=list)
    safe_resolutions: List[str] = field(default_factory=list)
    driver_info: str = ""
    camera_type: str = "unknown"
    physical_id: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "device": self.device,
            "format": self.format,
            "resolution": self.resolution,
            "supported_formats": self.supported_formats,
            "safe_resolutions": self.safe_resolutions,
            "driver_info": self.driver_info,
            "camera_type": self.camera_type,
            "physical_id": self.physical_id,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CameraConfig":
        """Deserialize from dictionary."""
        return cls(**data)
    
    def to_json(self) -> str:
        """Serialize to JSON."""
        return json.dumps(self.to_dict())
    
    @classmethod
    def from_json(cls, json_str: str) -> "CameraConfig":
        """Deserialize from JSON."""
        return cls.from_dict(json.loads(json_str))


@dataclass
class StreamHealth:
    """
    Health status of a stream worker.
    
    Attributes:
        camera_type: internal or external
        device: Device path
        state: running, restarting, failed, stopped
        pid: FFmpeg process ID
        format: Current pixel format
        resolution: Current resolution
        uptime_seconds: How long stream has been running
        restart_count: Number of restarts
        last_error: Last error message
        frame_count: Number of frames processed
        fps: Current frames per second
    """
    camera_type: str
    device: str
    state: str = "stopped"
    pid: Optional[int] = None
    format: str = ""
    resolution: str = ""
    uptime_seconds: float = 0.0
    restart_count: int = 0
    last_error: str = ""
    frame_count: int = 0
    fps: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "camera_type": self.camera_type,
            "device": self.device,
            "state": self.state,
            "pid": self.pid,
            "format": self.format,
            "resolution": self.resolution,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "restart_count": self.restart_count,
            "last_error": self.last_error,
            "frame_count": self.frame_count,
            "fps": round(self.fps, 1),
        }

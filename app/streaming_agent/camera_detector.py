"""
Camera Detection and Classification
Auto-detects internal (CSI) vs external (USB) cameras on Raspberry Pi 4.

Logic:
- INTERNAL_CAMERA_DEVICE and EXTERNAL_CAMERA_DEVICE env vars override auto-detection
- If 2 cameras found: first (/dev/video0) = internal, second = external
- If 1 camera: internal only
- If >2 cameras: use defaults with option to override
"""

from __future__ import annotations

import os
from glob import glob
from typing import NamedTuple, Optional


class CameraInfo(NamedTuple):
    """Information about a detected camera"""
    device_path: str
    camera_type: str  # "internal" or "external"
    index: int  # 0-based index among detected cameras


class CameraDetector:
    """Detects and classifies cameras for streaming"""
    
    def __init__(self):
        self.internal_device = os.getenv("INTERNAL_CAMERA_DEVICE", "/dev/video0")
        self.external_device = os.getenv("EXTERNAL_CAMERA_DEVICE", "/dev/video2")
    
    def detect_cameras(self) -> list[CameraInfo]:
        """
        Detect available video devices and classify them.
        
        Returns list of CameraInfo sorted by device path.
        """
        devices = sorted(glob("/dev/video*"))
        
        if not devices:
            return []
        
        # If env vars are explicitly set to specific paths, use them
        if os.getenv("INTERNAL_CAMERA_DEVICE") or os.getenv("EXTERNAL_CAMERA_DEVICE"):
            result = []
            if os.getenv("INTERNAL_CAMERA_DEVICE") and os.path.exists(self.internal_device):
                result.append(CameraInfo(
                    device_path=self.internal_device,
                    camera_type="internal",
                    index=0
                ))
            if os.getenv("EXTERNAL_CAMERA_DEVICE") and os.path.exists(self.external_device):
                result.append(CameraInfo(
                    device_path=self.external_device,
                    camera_type="external",
                    index=1 if result else 0
                ))
            return result
        
        # Auto-detection based on common Raspberry Pi patterns
        cameras = []
        for idx, device in enumerate(devices):
            # Heuristic: First camera is usually internal CSI
            # Second camera is usually external USB
            cam_type = "internal" if idx == 0 else "external"
            cameras.append(CameraInfo(
                device_path=device,
                camera_type=cam_type,
                index=idx
            ))
        
        return cameras
    
    def get_internal_camera(self) -> Optional[CameraInfo]:
        """Get internal camera if available"""
        for cam in self.detect_cameras():
            if cam.camera_type == "internal":
                return cam
        return None
    
    def get_external_camera(self) -> Optional[CameraInfo]:
        """Get external camera if available"""
        for cam in self.detect_cameras():
            if cam.camera_type == "external":
                return cam
        return None
    
    def get_cameras_for_streaming(self) -> dict[str, CameraInfo]:
        """
        Return dict with keys 'internal' and 'external' if cameras available.
        Missing cameras are simply omitted from the dict.
        """
        result = {}
        internal = self.get_internal_camera()
        if internal:
            result["internal"] = internal
        external = self.get_external_camera()
        if external:
            result["external"] = external
        return result

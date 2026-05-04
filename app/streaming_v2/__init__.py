"""
Production-Grade CCTV Streaming System (v2)

Parallel implementation to streaming_agent with zero modifications to existing code.
Provides self-healing, multi-camera streaming with enterprise-grade reliability.

Architecture:
- Camera Manager: Discovers and profiles cameras without locking devices
- Stream Workers: Independent FFmpeg supervisors per camera
- Stream Watchdog: Health monitoring and auto-recovery
- Device Safety: Ephemeral probing, no permanent locks
- MQTT Stability: Exponential backoff, session persistence
- Observability: Comprehensive logging and metrics

Feature Flag:
  USE_STREAMING_V2=true  (enables this module)
"""

from __future__ import annotations

__version__ = "2.0.0"

# MQTT is optional - only import if available
try:
    from .mqtt_stability import MQTTStabilityLayer, MQTTState
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    MQTTStabilityLayer = None
    MQTTState = None

# Public API
from .camera_manager import CameraManager
from .coordinator import CCTVStreamingCoordinator
from .device_safety import DeviceSafetyLayer, DeviceBusyError, DeviceAccessError
from .models import CameraConfig, StreamHealth
from .stream_watchdog import StreamWatchdog
from .stream_worker import StreamWorker

__all__ = [
    "CameraManager",
    "CCTVStreamingCoordinator",
    "DeviceSafetyLayer",
    "DeviceBusyError",
    "DeviceAccessError",
    "CameraConfig",
    "StreamHealth",
    "MQTTStabilityLayer",
    "MQTTState",
    "StreamWatchdog",
    "StreamWorker",
]

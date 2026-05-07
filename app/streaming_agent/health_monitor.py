"""
Health Monitor Module
Provides comprehensive health monitoring and reporting.
"""

from __future__ import annotations
import logging
import time
import psutil
from typing import Dict, Any, Optional

from .camera_registry import CameraRegistry
from .watchdog import PipelineWatchdog
from .reconnect_manager import ReconnectManager

logger = logging.getLogger(__name__)


class HealthMonitor:
    """Comprehensive health monitoring for the streaming system"""

    def __init__(self,
                 registry: CameraRegistry,
                 watchdog: PipelineWatchdog,
                 reconnect_manager: ReconnectManager):
        self.registry = registry
        self.watchdog = watchdog
        self.reconnect_manager = reconnect_manager

    def get_system_health(self) -> Dict[str, Any]:
        """Get comprehensive system health status"""
        return {
            "timestamp": time.time(),
            "system": self._get_system_stats(),
            "cameras": self._get_camera_health(),
            "streams": self._get_stream_health(),
            "overall_status": self._calculate_overall_status()
        }

    def _get_system_stats(self) -> Dict[str, Any]:
        """Get system resource statistics"""
        try:
            cpu_percent = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')

            return {
                "cpu_percent": cpu_percent,
                "memory_percent": memory.percent,
                "memory_used_mb": memory.used // (1024 * 1024),
                "memory_total_mb": memory.total // (1024 * 1024),
                "disk_percent": disk.percent,
                "disk_free_gb": disk.free // (1024 * 1024 * 1024),
                "uptime_seconds": time.time() - psutil.boot_time()
            }
        except Exception as e:
            logger.warning(f"Failed to get system stats: {e}")
            return {"error": str(e)}

    def _get_camera_health(self) -> Dict[str, Any]:
        """Get camera registry health"""
        devices = self.registry.get_active_devices()

        camera_stats = {
            "total_devices": len(devices),
            "by_backend": {},
            "by_type": {},
            "streaming_devices": 0
        }

        for device in devices:
            backend = device.classification.backend
            device_type = device.classification.device_type

            camera_stats["by_backend"][backend] = camera_stats["by_backend"].get(backend, 0) + 1
            camera_stats["by_type"][device_type] = camera_stats["by_type"].get(device_type, 0) + 1

            if device.stream_id:
                camera_stats["streaming_devices"] += 1

        return camera_stats

    def _get_stream_health(self) -> Dict[str, Any]:
        """Get streaming pipeline health"""
        watchdog_status = self.watchdog.get_health_status()
        active_streams = self.reconnect_manager.get_active_streams()

        stream_stats = {
            "total_streams": len(watchdog_status),
            "healthy_streams": 0,
            "unhealthy_streams": 0,
            "running_streams": 0,
            "streams": {}
        }

        for stream_name, status in watchdog_status.items():
            stream_stats["streams"][stream_name] = {
                "is_running": status["is_running"],
                "is_healthy": status["is_healthy"],
                "pid": status["pid"],
                "restart_count": status["restart_count"],
                "last_restart": status["last_restart"],
                "error_message": status.get("error_message"),
                "device_path": self.reconnect_manager.get_device_for_stream(stream_name)
            }

            if status["is_healthy"]:
                stream_stats["healthy_streams"] += 1
            else:
                stream_stats["unhealthy_streams"] += 1

            if status["is_running"]:
                stream_stats["running_streams"] += 1

        return stream_stats

    def _calculate_overall_status(self) -> str:
        """Calculate overall system health status"""
        try:
            system_stats = self._get_system_stats()
            camera_stats = self._get_camera_health()
            stream_stats = self._get_stream_health()

            # Critical system resources
            if system_stats.get("memory_percent", 0) > 90:
                return "critical_memory"
            if system_stats.get("cpu_percent", 0) > 95:
                return "critical_cpu"
            if system_stats.get("disk_percent", 0) > 95:
                return "critical_disk"

            # Streaming health
            total_streams = stream_stats["total_streams"]
            healthy_streams = stream_stats["healthy_streams"]

            if total_streams == 0:
                return "no_streams"
            elif healthy_streams == 0:
                return "all_streams_failed"
            elif healthy_streams < total_streams:
                return "degraded"
            else:
                return "healthy"

        except Exception as e:
            logger.exception("Error calculating overall status")
            return "unknown_error"

    def get_detailed_report(self) -> str:
        """Generate a detailed human-readable health report"""
        health = self.get_system_health()

        report_lines = [
            "=== Streaming Agent Health Report ===",
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(health['timestamp']))}",
            f"Overall Status: {health['overall_status']}",
            "",
            "SYSTEM RESOURCES:",
        ]

        sys_stats = health["system"]
        if "error" not in sys_stats:
            report_lines.extend([
                f"  CPU: {sys_stats['cpu_percent']:.1f}%",
                f"  Memory: {sys_stats['memory_percent']:.1f}% ({sys_stats['memory_used_mb']}MB / {sys_stats['memory_total_mb']}MB)",
                f"  Disk: {sys_stats['disk_percent']:.1f}% free ({sys_stats['disk_free_gb']}GB)",
                f"  Uptime: {sys_stats['uptime_seconds']:.0f} seconds",
            ])
        else:
            report_lines.append(f"  Error getting system stats: {sys_stats['error']}")

        report_lines.extend([
            "",
            "CAMERA STATUS:",
        ])

        cam_stats = health["cameras"]
        report_lines.extend([
            f"  Total cameras: {cam_stats['total_devices']}",
            f"  Streaming cameras: {cam_stats['streaming_devices']}",
        ])

        if cam_stats["by_backend"]:
            report_lines.append("  By backend:")
            for backend, count in cam_stats["by_backend"].items():
                report_lines.append(f"    {backend}: {count}")

        if cam_stats["by_type"]:
            report_lines.append("  By type:")
            for cam_type, count in cam_stats["by_type"].items():
                report_lines.append(f"    {cam_type}: {count}")

        report_lines.extend([
            "",
            "STREAM STATUS:",
        ])

        stream_stats = health["streams"]
        report_lines.extend([
            f"  Total streams: {stream_stats['total_streams']}",
            f"  Running streams: {stream_stats['running_streams']}",
            f"  Healthy streams: {stream_stats['healthy_streams']}",
            f"  Unhealthy streams: {stream_stats['unhealthy_streams']}",
        ])

        if stream_stats["streams"]:
            report_lines.append("  Stream details:")
            for stream_name, status in stream_stats["streams"].items():
                health_status = "✓" if status["is_healthy"] else "✗"
                running_status = "running" if status["is_running"] else "stopped"
                device = status.get("device_path", "unknown")
                restarts = status["restart_count"]
                report_lines.append(f"    {stream_name}: {health_status} {running_status} (device: {device}, restarts: {restarts})")

        return "\n".join(report_lines)</content>
<parameter name="filePath">/home/hassaanqazi/Documents/smart-locker-hardware/app/streaming_agent/health_monitor.py
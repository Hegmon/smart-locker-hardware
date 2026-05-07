"""
Pipeline Factory Module
Creates appropriate pipeline instances based on camera classification.
"""

from __future__ import annotations
import logging
from typing import Optional

from .camera_classifier import CameraClassification
from .camera_capabilities import CameraCapabilities
from .pipelines.base_pipeline import BasePipeline, PipelineConfig
from .pipelines.libcamera_pipeline import LibcameraPipeline
from .pipelines.v4l2_pipeline import V4L2Pipeline
from .pipelines.mjpeg_pipeline import MJPEGPipeline
from .pipelines.h264_pipeline import H264PassthroughPipeline

logger = logging.getLogger(__name__)


class PipelineFactory:
    """Factory for creating camera streaming pipelines"""

    @staticmethod
    def create_pipeline(
        device_path: str,
        stream_name: str,
        classification: CameraClassification,
        capabilities: CameraCapabilities,
        rtsp_host: str = "127.0.0.1",
        rtsp_port: int = 8554
    ) -> Optional[BasePipeline]:
        """
        Create appropriate pipeline for camera based on classification and capabilities.

        Args:
            device_path: Camera device path
            stream_name: RTSP stream name
            classification: Camera classification
            capabilities: Camera capabilities
            rtsp_host: RTSP server host
            rtsp_port: RTSP server port

        Returns:
            Configured pipeline instance or None if unsupported
        """
        # Create base config
        config = PipelineFactory._create_pipeline_config(
            device_path, stream_name, capabilities, rtsp_host, rtsp_port
        )

        # Select pipeline type based on backend and capabilities
        pipeline = PipelineFactory._select_pipeline(config, classification, capabilities)

        if pipeline:
            logger.info(f"Created {classification.backend} pipeline for {device_path}")
        else:
            logger.warning(f"No suitable pipeline for {device_path} (backend: {classification.backend})")

        return pipeline

    @staticmethod
    def _create_pipeline_config(
        device_path: str,
        stream_name: str,
        capabilities: CameraCapabilities,
        rtsp_host: str,
        rtsp_port: int
    ) -> PipelineConfig:
        """Create pipeline configuration from capabilities"""
        # Determine resolution
        resolution = PipelineFactory._select_resolution(capabilities)

        # Determine framerate
        framerate = 25  # Default
        if "h264" in capabilities.capabilities:
            framerate = 30  # Higher framerate for hardware encoded

        # Determine bitrate
        bitrate = "1000k"  # Default
        if "mjpeg" in capabilities.capabilities:
            bitrate = "2000k"  # MJPEG can handle higher bitrate

        return PipelineConfig(
            device_path=device_path,
            stream_name=stream_name,
            rtsp_host=rtsp_host,
            rtsp_port=rtsp_port,
            resolution=resolution,
            framerate=framerate,
            bitrate=bitrate
        )

    @staticmethod
    def _select_resolution(capabilities: CameraCapabilities) -> str:
        """Select appropriate resolution from capabilities"""
        resolutions = capabilities.supported_resolutions

        if not resolutions:
            return "640x480"  # Safe default

        # Preference order for different camera types
        preferred_resolutions = [
            "1280x720",  # 720p
            "1920x1080", # 1080p
            "640x480",   # 480p
            "320x240"    # Minimum fallback
        ]

        for pref_res in preferred_resolutions:
            if pref_res in resolutions:
                return pref_res

        # Return first available resolution
        return resolutions[0]

    @staticmethod
    def _select_pipeline(
        config: PipelineConfig,
        classification: CameraClassification,
        capabilities: CameraCapabilities
    ) -> Optional[BasePipeline]:
        """Select and create appropriate pipeline instance"""

        backend = classification.backend
        caps = capabilities.capabilities

        # Libcamera pipeline for CSI cameras
        if backend == "libcamera":
            return LibcameraPipeline(config)

        # H264 passthrough for cameras with hardware H264
        elif backend == "h264_passthrough" or "h264" in caps:
            return H264PassthroughPipeline(config)

        # MJPEG pipeline for MJPEG cameras
        elif backend == "mjpeg" or "mjpeg" in caps:
            return MJPEGPipeline(config)

        # Standard V4L2 pipeline as fallback
        elif backend == "v4l2":
            return V4L2Pipeline(config)

        # Unknown backend
        else:
            logger.warning(f"Unsupported backend: {backend}")
            return None</content>
<parameter name="filePath">/home/hassaanqazi/Documents/smart-locker-hardware/app/streaming_agent/pipeline_factory.py
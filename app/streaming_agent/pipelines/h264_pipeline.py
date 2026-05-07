"""
H264 Passthrough Pipeline Module
Pipeline for cameras with hardware H264 encoding - minimal processing.
"""

from __future__ import annotations
import logging
from typing import List

from .v4l2_pipeline import V4L2Pipeline
from .base_pipeline import PipelineConfig

logger = logging.getLogger(__name__)


class H264PassthroughPipeline(V4L2Pipeline):
    """Pipeline for cameras with hardware H264 encoding"""

    def __init__(self, config: PipelineConfig):
        # Force H264 format
        super().__init__(config, preferred_format="h264")

    def get_command(self) -> List[str]:
        """Build H264 passthrough command with minimal processing"""
        device_path = self.config.device_path
        stream_name = self.config.stream_name

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-f", "v4l2",
            "-input_format", "h264",  # Force H264 input
            "-framerate", str(self.config.framerate),
            "-video_size", self.config.resolution,
            "-i", device_path,
            "-c:v", "copy",  # Copy stream without re-encoding
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
        ]

        # Output RTSP URL
        rtsp_url = f"rtsp://{self.config.rtsp_host}:{self.config.rtsp_port}/{stream_name}"
        cmd.append(rtsp_url)

        return cmd

    def validate_config(self) -> bool:
        """Validate H264 passthrough configuration"""
        # Basic validation
        if not super().validate_config():
            return False

        # H264 passthrough requires camera to support H264
        # This should be verified by capabilities detection
        logger.info(f"H264 passthrough pipeline for {self.config.device_path}")
        return True
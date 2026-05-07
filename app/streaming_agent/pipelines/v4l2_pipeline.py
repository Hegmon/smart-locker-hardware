"""
V4L2 Pipeline Module
Handles USB camera streaming using V4L2/ffmpeg.
"""

from __future__ import annotations
import logging
from typing import List

from .base_pipeline import BasePipeline, PipelineConfig

logger = logging.getLogger(__name__)


class V4L2Pipeline(BasePipeline):
    """Pipeline for USB/V4L2 cameras using ffmpeg"""

    def __init__(self, config: PipelineConfig, preferred_format: str = ""):
        super().__init__(config)
        self.preferred_format = preferred_format

    def get_command(self) -> List[str]:
        """Build ffmpeg V4L2 command"""
        device_path = self.config.device_path
        stream_name = self.config.stream_name

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-f", "v4l2",
            "-framerate", str(self.config.framerate),
            "-video_size", self.config.resolution,
        ]

        # Add input format if specified
        if self.preferred_format:
            cmd.extend(["-input_format", self.preferred_format])

        # Input device
        cmd.extend(["-i", device_path])

        # Encoding options
        cmd.extend([
            "-c:v", self.config.codec,
            "-preset", self.config.preset,
            "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-g", "10",
            "-keyint_min", "10",
        ])

        # Bitrate control
        if self.config.bitrate:
            cmd.extend(["-b:v", self.config.bitrate])

        # RTSP output
        cmd.extend([
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
        ])

        # Output RTSP URL
        rtsp_url = f"rtsp://{self.config.rtsp_host}:{self.config.rtsp_port}/{stream_name}"
        cmd.append(rtsp_url)

        return cmd

    def validate_config(self) -> bool:
        """Validate V4L2 pipeline configuration"""
        import shutil
        if not shutil.which("ffmpeg"):
            logger.error("ffmpeg not found in PATH")
            return False

        # Check if device exists
        import os
        if not os.path.exists(self.config.device_path):
            logger.error(f"Device does not exist: {self.config.device_path}")
            return False

        # Check resolution format
        if 'x' not in self.config.resolution:
            logger.error(f"Invalid resolution format: {self.config.resolution}")
            return False

        return True
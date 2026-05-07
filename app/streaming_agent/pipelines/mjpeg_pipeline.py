"""
MJPEG Pipeline Module
Optimized pipeline for cameras supporting MJPEG compression.
"""

from __future__ import annotations
import logging
from typing import List

from .v4l2_pipeline import V4L2Pipeline
from .base_pipeline import PipelineConfig

logger = logging.getLogger(__name__)


class MJPEGPipeline(V4L2Pipeline):
    """Pipeline optimized for MJPEG cameras"""

    def __init__(self, config: PipelineConfig):
        # Force MJPEG format
        super().__init__(config, preferred_format="mjpeg")

    def get_command(self) -> List[str]:
        """Build MJPEG-optimized ffmpeg command"""
        device_path = self.config.device_path
        stream_name = self.config.stream_name

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-f", "v4l2",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-analyzeduration", "0",
            "-probesize", "32",
            "-input_format", "mjpeg",  # Force MJPEG input
            "-framerate", str(self.config.framerate),
            "-video_size", self.config.resolution,
            "-i", device_path,
        ]

        # For MJPEG, we can often copy the stream directly
        # Check if we can do hardware-accelerated decoding
        cmd.extend([
            "-c:v", "mjpeg",  # MJPEG decoder
            "-q:v", "2",      # Quality setting for MJPEG
        ])

        # Re-encode to H264 for RTSP
        cmd.extend([
            "-c:v", "libx264",
            "-preset", "ultrafast",
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
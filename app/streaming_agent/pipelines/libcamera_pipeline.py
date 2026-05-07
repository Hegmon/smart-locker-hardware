"""
Libcamera Pipeline Module
Handles CSI camera streaming using libcamera-vid or rpicam-vid.
"""

from __future__ import annotations
import logging
from typing import List

from .base_pipeline import BasePipeline, PipelineConfig

logger = logging.getLogger(__name__)


class LibcameraPipeline(BasePipeline):
    """Pipeline for CSI/libcamera cameras using libcamera-vid or rpicam-vid"""

    def get_command(self) -> List[str]:
        """Build libcamera command with ffmpeg consumer"""
        device_path = self.config.device_path
        stream_name = self.config.stream_name

        # Try rpicam-vid first (preferred for Raspberry Pi)
        rpicam_cmd = self._build_rpicam_command()

        # Build ffmpeg consumer
        ffmpeg_cmd = self._build_ffmpeg_command()

        # Combine with pipe
        full_cmd = rpicam_cmd + ["|"] + ffmpeg_cmd

        return full_cmd

    def _build_rpicam_command(self) -> List[str]:
        """Build rpicam-vid command"""
        width, height = self.config.resolution.split('x')

        cmd = [
            "rpicam-vid",
            "-t", "0",  # infinite duration
            "--width", width,
            "--height", height,
            "--framerate", str(self.config.framerate),
            "--codec", "h264",
            "--inline",  # inline SPS/PPS
            "--nopreview",  # no preview window
            "-o", "-",  # output to stdout for piping
        ]

        # Add bitrate control if specified
        if self.config.bitrate:
            cmd.extend(["--bitrate", self.config.bitrate])

        return cmd

    def _build_ffmpeg_command(self) -> List[str]:
        """Build ffmpeg command to consume libcamera output"""
        stream_name = self.config.stream_name

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-f", "h264",  # Input from rpicam-vid
            "-i", "-",  # Read from stdin (piped)
            "-c:v", "copy",  # Copy stream without re-encoding
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
        ]

        # Add low-latency flags for consumer ffmpeg
        cmd[0:0] = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-analyzeduration", "0",
            "-probesize", "32",
        ]

        # Output RTSP URL
        rtsp_url = f"rtsp://{self.config.rtsp_host}:{self.config.rtsp_port}/{stream_name}"
        cmd.append(rtsp_url)

        return cmd

    def validate_config(self) -> bool:
        """Validate libcamera pipeline configuration"""
        # Check if rpicam-vid or libcamera-vid is available
        import shutil
        if not (shutil.which("rpicam-vid") or shutil.which("libcamera-vid")):
            logger.error("Neither rpicam-vid nor libcamera-vid found in PATH")
            return False

        # Check resolution format
        if 'x' not in self.config.resolution:
            logger.error(f"Invalid resolution format: {self.config.resolution}")
            return False

        return True
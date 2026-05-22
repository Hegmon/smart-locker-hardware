from app.streaming_agent.camera_roles import assign_camera_roles
from app.streaming_agent.camera_controls import CameraControlManager
from app.streaming_agent.ffmpeg_builder import (
    QR_FRAME_CHANNELS,
    QR_FRAME_HEIGHT,
    QR_FRAME_WIDTH,
    build_ffmpeg_command,
    build_public_stream_urls,
)
from app.streaming_agent.frame_buffer import SharedFrameBuffer
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager
from app.streaming_agent.stream_process import StreamProcess
from app.streaming_agent.watchdog import StreamWatchdog


logger = LoggingManager.get_logger(__name__)
INTERNAL_FRAME_WIDTH = 640
INTERNAL_FRAME_HEIGHT = 480
INTERNAL_FRAME_CHANNELS = 3


class StreamingManager:
    """Central orchestration layer for all camera streams."""

    def __init__(self):
        self.streams = {}
        self.watchdog = None
        self.frame_buffers = {}
        self.camera_roles = {}
        self.camera_controls = CameraControlManager()

    def initialize(self):
        logger.info("Initializing streams")
        self.streams.clear()
        self.frame_buffers.clear()

        camera_roles = assign_camera_roles()
        self.camera_roles = camera_roles
        for role, camera in camera_roles.items():
            if camera is None:
                logger.warning("Missing camera for role %s", role)
                continue

            video_device = camera["video_device"]
            if role == "external":
                self.camera_controls.enable_autofocus(video_device, reason="external camera startup", force=True)
            logger.info("Building ffmpeg command for %s camera at %s", role, video_device)
            is_external = role == "external"
            frame_width = QR_FRAME_WIDTH if is_external else INTERNAL_FRAME_WIDTH
            frame_height = QR_FRAME_HEIGHT if is_external else INTERNAL_FRAME_HEIGHT
            frame_channels = QR_FRAME_CHANNELS if is_external else INTERNAL_FRAME_CHANNELS
            frame_pipe = is_external or role == "internal"
            frame_buffer = (
                SharedFrameBuffer(width=frame_width, height=frame_height, channels=frame_channels)
                if frame_pipe
                else None
            )
            ffmpeg_command = build_ffmpeg_command(
                video_device,
                camera_role=role,
                frame_pipe=frame_pipe,
                frame_width=frame_width,
                frame_height=frame_height,
            )
            self.streams[role] = StreamProcess(
                name=f"{role.capitalize()} Camera Stream",
                ffmpeg_command=ffmpeg_command,
                frame_buffer=frame_buffer,
            )
            if frame_buffer:
                self.frame_buffers[role] = frame_buffer
                logger.info(
                    "Stream for %s camera initialized with raw frame pipe %sx%sx%s frame_size=%s",
                    role,
                    frame_buffer.width,
                    frame_buffer.height,
                    frame_buffer.channels,
                    frame_buffer.frame_size,
                )
            else:
                logger.info("Stream for %s camera initialized without raw frame pipe", role)

        if not self.streams:
            logger.warning("No streaming cameras were initialized")

    def get_frame_buffer(self, role="internal"):
        return self.frame_buffers.get(role)

    def get_camera_device(self, role):
        camera = self.camera_roles.get(role) or {}
        return camera.get("video_device")

    def start_all(self):
        logger.info("Starting all streams")
        for role, stream in self.streams.items():
            logger.info("Starting stream for %s camera", role)
            stream.start()

        if self.streams and self.watchdog is None:
            self.watchdog = StreamWatchdog(
                stream_registry=self.streams,
                check_interval=5,
                restart_cooldown=3,
            )
            self.watchdog.start()

    def stop_all(self):
        logger.info("Stopping all streams")
        if self.watchdog:
            self.watchdog.stop()
            self.watchdog = None

        for role, stream in self.streams.items():
            logger.info("Stopping stream for %s camera", role)
            stream.stop()

    def restart_all(self, reason="manual restart"):
        logger.warning("Restarting all streams: %s", reason)
        for role, stream in self.streams.items():
            logger.info("Restarting stream for %s camera", role)
            stream.stop()
            stream.restart_attempts = 0
            stream.start()

    def get_stream_status(self):
        status = {}
        for role, stream in self.streams.items():
            status[role] = {
                "running": stream.is_running(),
                "name": stream.name,
                "pid": stream.process.pid if stream.process else None,
                "rtsp_url": stream.rtsp_url,
                "urls": build_public_stream_urls(role),
            }
        return status

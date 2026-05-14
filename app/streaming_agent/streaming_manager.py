from app.streaming_agent.camera_roles import assign_camera_roles
from app.streaming_agent.ffmpeg_builder import build_ffmpeg_command
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager
from app.streaming_agent.stream_process import StreamProcess
from app.streaming_agent.watchdog import StreamWatchdog


logger = LoggingManager.get_logger(__name__)


class StreamingManager:
    """Central orchestration layer for all camera streams."""

    def __init__(self):
        self.streams = {}
        self.watchdog = None

    def initialize(self):
        logger.info("Initializing streams")
        self.streams.clear()

        camera_roles = assign_camera_roles()
        for role, camera in camera_roles.items():
            if camera is None:
                logger.warning("Missing camera for role %s", role)
                continue

            video_device = camera["video_device"]
            logger.info("Building ffmpeg command for %s camera at %s", role, video_device)
            ffmpeg_command = build_ffmpeg_command(video_device, camera_role=role)
            self.streams[role] = StreamProcess(
                name=f"{role.capitalize()} Camera Stream",
                ffmpeg_command=ffmpeg_command,
            )
            logger.info("Stream for %s camera initialized", role)

        if not self.streams:
            logger.warning("No streaming cameras were initialized")

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
            }
        return status
